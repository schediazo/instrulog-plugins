"""
Hach DR6000 Spectrophotometer — File Watch Plugin
==================================================
Polls a local folder, FTP, SFTP, or SMB network share for CSV files
exported by the DR6000 after each measurement session.

The DR6000 exports one CSV file per session to a configured destination
(USB drive, FTP share, or SMB/network drive). Each CSV may contain
multiple rows — one per test run (Boron, COD, Iron, etc.). Each row
becomes a separate InstruLog reading, keyed by the Parameter/test name.

CSV format (UTF-16 LE, BOM-prefixed):
  Row 1:  instrument info   (DR6000, S/N ...)
  Row 2:  column headers    (Parameter, Date, Result, Unit, Name, Wavelength1, Value1 ...)
  Row 3+: data rows

Config keys (set on the instrument record in InstruLog — stored in file_config):
  path          — folder to watch: C:\\DR_DATA\\DataLog  or  /mnt/dr6000
  mode          — folder_watch  (default; one file per session)
                  single_file   (instrument always overwrites the same file)
  format        — csv  (DR6000 exports CSV only)
  encoding      — utf-16  (DR6000 default; change if your firmware differs)
  protocol      — local | ftp | sftp | smb  (default: local)
  ip            — remote host IP for ftp/sftp/smb  (stored in instrument host field)
  port          — remote port  (21 / 22 / 445)
  username      — remote username  (stored in file_config)
  auth_key      — password  (decrypted before being passed here)
  smb_share     — SMB share name  e.g. DR_DATA  (smb only)

Metrics returned per row (keyed by test/Parameter name):
  <TestName>             — numeric result  (e.g. Boron, COD_ULR, Iron_FerroMo)
  <TestName>_abs         — absorbance at primary wavelength
  <TestName>_wavelength  — primary wavelength (nm)
"""

import asyncio
import csv
import ftplib
import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Fake async reader/writer so stream_engine's read loop works ───────────────

class _FakeReader:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()

    async def read(self, _n: int) -> bytes:
        return await self._q.get()

    async def put(self, data: bytes):
        await self._q.put(data)


class _FakeWriter:
    def write(self, _): pass
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass


# ── Main plugin class ─────────────────────────────────────────────────────────

class HachDR6000Integration:

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._reader = _FakeReader()
        self._writer = _FakeWriter()
        self._seen_files: set = set()        # folder_watch: already processed
        self._last_mtime: float = 0.0        # single_file: last mod time
        self._last_size:  int   = 0          # single_file: last size
        self._poll_seconds: float = 2.0

    @property
    def instrument_type(self) -> str:
        return "hach_dr6000_file"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config  = config
        self._poll_seconds = config.get("poll_interval_ms", 5000) / 1000.0
        path     = config.get("path", "").strip()
        protocol = config.get("protocol", "local")

        if not path:
            raise ValueError("DR6000: 'path' must be set in file_config (folder or file path)")

        logger.info("DR6000: protocol=%s  path=%s  mode=%s",
                    protocol, path, config.get("mode", "folder_watch"))

        # Quick connectivity check for local
        if protocol == "local":
            mode = config.get("mode", "folder_watch")
            if mode == "folder_watch" and not os.path.isdir(path):
                raise FileNotFoundError(f"DR6000: folder not found: {path}")
            if mode == "single_file" and not os.path.isfile(path):
                raise FileNotFoundError(f"DR6000: file not found: {path}")

        # Start background poller
        asyncio.create_task(self._poll_loop())
        return self._reader, self._writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        """stream_engine calls this with whatever bytes _poll_loop fed in."""
        try:
            return json.loads(raw_bytes.decode("utf-8"))
        except Exception as e:
            return {"status": "error", "message": f"DR6000 decode error: {e}"}

    # ── Poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        mode = self._config.get("mode", "folder_watch")
        while True:
            try:
                if mode == "single_file":
                    await self._poll_single()
                else:
                    await self._poll_folder()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("DR6000: poll error: %s", e)
            await asyncio.sleep(self._poll_seconds)

    async def _poll_folder(self):
        path     = self._config.get("path", "")
        encoding = self._config.get("encoding", "utf-16")
        loop     = asyncio.get_event_loop()

        filenames = await loop.run_in_executor(None, self._list_files, path)
        for fname in filenames:
            if fname in self._seen_files:
                continue
            fpath   = os.path.join(path, fname) if self._config.get("protocol", "local") == "local" else fname
            content = await loop.run_in_executor(None, self._read_file, fpath, encoding)
            if content is None:
                continue
            rows = self._parse_csv(content, fname)
            for row in rows:
                await self._reader.put(json.dumps(row).encode("utf-8"))
            self._seen_files.add(fname)

    async def _poll_single(self):
        path     = self._config.get("path", "")
        encoding = self._config.get("encoding", "utf-16")
        loop     = asyncio.get_event_loop()

        try:
            stat  = os.stat(path)
            mtime = stat.st_mtime
            size  = stat.st_size
        except Exception:
            return

        if mtime == self._last_mtime and size == self._last_size:
            return

        self._last_mtime = mtime
        self._last_size  = size

        content = await loop.run_in_executor(None, self._read_file, path, encoding)
        if content is None:
            return
        rows = self._parse_csv(content, os.path.basename(path))
        for row in rows:
            await self._reader.put(json.dumps(row).encode("utf-8"))

    # ── File listing ──────────────────────────────────────────────────────────

    def _list_files(self, folder: str) -> List[str]:
        protocol = self._config.get("protocol", "local")
        fmt      = self._config.get("format", "csv")
        ext      = f".{fmt}"
        try:
            if protocol == "local":
                return sorted(
                    f for f in os.listdir(folder)
                    if f.lower().endswith(ext) and not f.startswith(".")
                )
            elif protocol == "ftp":
                return self._list_ftp(folder, ext)
            elif protocol == "sftp":
                return self._list_sftp(folder, ext)
            elif protocol == "smb":
                return self._list_smb(folder, ext)
        except Exception as e:
            logger.error("DR6000: list_files error: %s", e)
        return []

    def _list_ftp(self, folder: str, ext: str) -> List[str]:
        ftp = self._ftp_conn()
        ftp.cwd(folder)
        names = sorted(n for n in ftp.nlst() if n.lower().endswith(ext))
        ftp.quit()
        return names

    def _list_sftp(self, folder: str, ext: str) -> List[str]:
        ssh, sftp = self._sftp_conn()
        names = sorted(n for n in sftp.listdir(folder) if n.lower().endswith(ext))
        ssh.close()
        return names

    def _list_smb(self, folder: str, ext: str) -> List[str]:
        conn, share = self._smb_conn()
        names = sorted(
            f.filename for f in conn.listPath(share, folder)
            if f.filename.lower().endswith(ext)
        )
        conn.close()
        return names

    # ── File reading ──────────────────────────────────────────────────────────

    def _read_file(self, path: str, encoding: str) -> Optional[str]:
        protocol = self._config.get("protocol", "local")
        try:
            if protocol == "local":
                # Auto-detect utf-16 BOM, fall back to configured encoding
                with open(path, "rb") as fb:
                    raw = fb.read(4)
                detected = encoding
                if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
                    detected = "utf-16"
                elif raw[:3] == b'\xef\xbb\xbf':
                    detected = "utf-8-sig"
                with open(path, "r", encoding=detected, errors="ignore") as f:
                    return f.read()
            elif protocol == "ftp":
                ftp = self._ftp_conn()
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {path}", buf.write)
                ftp.quit()
                return buf.getvalue().decode(encoding, errors="ignore")
            elif protocol == "sftp":
                ssh, sftp = self._sftp_conn()
                with sftp.file(path, "rb") as f:
                    data = f.read()
                ssh.close()
                return data.decode(encoding, errors="ignore")
            elif protocol == "smb":
                conn, share = self._smb_conn()
                buf = io.BytesIO()
                conn.retrieveFile(share, path, buf)
                conn.close()
                return buf.getvalue().decode(encoding, errors="ignore")
        except Exception as e:
            logger.error("DR6000: read_file %s: %s", path, e)
        return None

    # ── Connection helpers ────────────────────────────────────────────────────

    def _ftp_conn(self):
        ftp = ftplib.FTP()
        ftp.connect(
            self._config.get("ip") or self._config.get("host", ""),
            int(self._config.get("port") or 21),
            timeout=10,
        )
        ftp.login(
            self._config.get("username", ""),
            self._config.get("auth_key", ""),
        )
        return ftp

    def _sftp_conn(self):
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self._config.get("ip") or self._config.get("host", ""),
            port=int(self._config.get("port") or 22),
            username=self._config.get("username", ""),
            password=self._config.get("auth_key", ""),
            timeout=10,
        )
        return ssh, ssh.open_sftp()

    def _smb_conn(self):
        from smb.SMBConnection import SMBConnection
        share = self._config.get("smb_share", "")
        host  = self._config.get("ip") or self._config.get("host", "")
        conn  = SMBConnection(
            self._config.get("username", ""),
            self._config.get("auth_key", ""),
            "instrulog", host,
            use_ntlm_v2=True,
        )
        conn.connect(host, int(self._config.get("port") or 445), timeout=10)
        return conn, share

    # ── CSV parsing ───────────────────────────────────────────────────────────

    def _parse_csv(self, content: str, filename: str) -> List[Dict[str, Any]]:
        """
        DR6000 CSV (UTF-16):
          Row 0: instrument header  (DR6000, S/N ...)
          Row 1: column headers     (Parameter, Date, ..., Result, Unit, ..., Wavelength1, Value1, Unit1 ...)
          Row 2+: data rows
        Each row = one test. Parameter column = test name (Boron, COD ULR, etc.)
        """
        results = []
        try:
            reader  = csv.reader(io.StringIO(content))
            headers = None
            for raw_row in reader:
                row = [c.strip() for c in raw_row]
                if not any(row):
                    continue
                # Detect header row
                if headers is None:
                    joined = ",".join(row).lower()
                    if "parameter" in joined and ("result" in joined or "value1" in joined):
                        headers = row
                    continue

                d = {k.rstrip(':'): v for k, v in zip(headers, row)}
                parameter = d.get("Parameter") or d.get("parameter", "")
                if not parameter or parameter in ("Parameter", "DR6000", "DR3900"):
                    continue

                result_val = _parse_float(d.get("Result") or d.get("result", ""))
                if result_val is None:
                    continue

                unit     = (d.get("Unit") or d.get("unit") or "mg/L").strip()
                test_key = _safe_key(parameter)

                metrics: Dict[str, Any] = {
                    test_key: {"value": result_val, "unit": unit}
                }

                # Absorbance / wavelength columns (Value1/Wavelength1 etc.)
                for i in range(1, 5):
                    wl  = _parse_float(d.get(f"Wavelength{i}", ""))
                    val = _parse_float(d.get(f"Value{i}", ""))
                    u   = (d.get(f"Unit{i}") or "Abs").strip()
                    if val is not None:
                        suffix = f"_{int(wl)}nm" if wl else f"_{i}"
                        metrics[f"{test_key}_abs{suffix}"] = {"value": val, "unit": u}

                results.append({
                    "status": "success",
                    "instrument_type": self.instrument_type,
                    "source_file": filename,
                    "test_name": parameter,
                    "metrics": metrics,
                })

        except Exception as e:
            logger.error("DR6000: parse_csv error in %s: %s", filename, e)
            results.append({
                "status": "error",
                "message": f"DR6000 parse error in {filename}: {e}",
            })
        return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_float(s: str) -> Optional[float]:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None

def _safe_key(name: str) -> str:
    """Convert test name to a clean metric key: 'COD ULR' → 'COD_ULR'"""
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")