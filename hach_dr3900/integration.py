"""
Hach DR3900 Spectrophotometer — File Watch Plugin
==================================================
Same as DR6000 plugin but for DR3900 exports.

DR3900 CSV format is identical to DR6000 (UTF-16 LE, same column names).
The only differences are the instrument header row and the S/N prefix.
All config keys are the same — see DR6000 docstring.
"""

import asyncio
import csv
import ftplib
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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


class HachDR3900Integration:

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._reader = _FakeReader()
        self._writer = _FakeWriter()
        self._seen_files: set = set()
        self._last_mtime: float = 0.0
        self._last_size:  int   = 0
        self._poll_seconds: float = 2.0

    @property
    def instrument_type(self) -> str:
        return "hach_dr3900_file"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        self._poll_seconds = config.get("poll_interval_ms", 5000) / 1000.0
        path     = config.get("path", "").strip()
        protocol = config.get("protocol", "local")

        if not path:
            raise ValueError("DR3900: 'path' must be set in file_config")

        logger.info("DR3900: protocol=%s  path=%s  mode=%s",
                    protocol, path, config.get("mode", "folder_watch"))

        if protocol == "local":
            mode = config.get("mode", "folder_watch")
            if mode == "folder_watch" and not os.path.isdir(path):
                raise FileNotFoundError(f"DR3900: folder not found: {path}")
            if mode == "single_file" and not os.path.isfile(path):
                raise FileNotFoundError(f"DR3900: file not found: {path}")

        asyncio.create_task(self._poll_loop())
        return self._reader, self._writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        try:
            return json.loads(raw_bytes.decode("utf-8"))
        except Exception as e:
            return {"status": "error", "message": f"DR3900 decode error: {e}"}

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
                logger.error("DR3900: poll error: %s", e)
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

    def _list_files(self, folder: str) -> List[str]:
        protocol = self._config.get("protocol", "local")
        ext      = f".{self._config.get('format', 'csv')}"
        try:
            if protocol == "local":
                return sorted(f for f in os.listdir(folder) if f.lower().endswith(ext) and not f.startswith("."))
            elif protocol == "ftp":
                ftp = self._ftp_conn(); ftp.cwd(folder)
                names = sorted(n for n in ftp.nlst() if n.lower().endswith(ext))
                ftp.quit(); return names
            elif protocol == "sftp":
                ssh, sftp = self._sftp_conn()
                names = sorted(n for n in sftp.listdir(folder) if n.lower().endswith(ext))
                ssh.close(); return names
            elif protocol == "smb":
                conn, share = self._smb_conn()
                names = sorted(f.filename for f in conn.listPath(share, folder) if f.filename.lower().endswith(ext))
                conn.close(); return names
        except Exception as e:
            logger.error("DR3900: list_files: %s", e)
        return []

    def _read_file(self, path: str, encoding: str) -> Optional[str]:
        protocol = self._config.get("protocol", "local")
        try:
            if protocol == "local":
                with open(path, "r", encoding=encoding, errors="ignore") as f:
                    return f.read()
            elif protocol == "ftp":
                ftp = self._ftp_conn(); buf = io.BytesIO()
                ftp.retrbinary(f"RETR {path}", buf.write); ftp.quit()
                return buf.getvalue().decode(encoding, errors="ignore")
            elif protocol == "sftp":
                ssh, sftp = self._sftp_conn()
                with sftp.file(path, "rb") as f: data = f.read()
                ssh.close(); return data.decode(encoding, errors="ignore")
            elif protocol == "smb":
                conn, share = self._smb_conn(); buf = io.BytesIO()
                conn.retrieveFile(share, path, buf); conn.close()
                return buf.getvalue().decode(encoding, errors="ignore")
        except Exception as e:
            logger.error("DR3900: read_file %s: %s", path, e)
        return None

    def _ftp_conn(self):
        ftp = ftplib.FTP()
        ftp.connect(self._config.get("ip") or self._config.get("host", ""), int(self._config.get("port") or 21), timeout=10)
        ftp.login(self._config.get("username", ""), self._config.get("auth_key", ""))
        return ftp

    def _sftp_conn(self):
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(self._config.get("ip") or self._config.get("host", ""),
                    port=int(self._config.get("port") or 22),
                    username=self._config.get("username", ""),
                    password=self._config.get("auth_key", ""), timeout=10)
        return ssh, ssh.open_sftp()

    def _smb_conn(self):
        from smb.SMBConnection import SMBConnection
        host  = self._config.get("ip") or self._config.get("host", "")
        share = self._config.get("smb_share", "")
        conn  = SMBConnection(self._config.get("username", ""), self._config.get("auth_key", ""),
                              "instrulog", host, use_ntlm_v2=True)
        conn.connect(host, int(self._config.get("port") or 445), timeout=10)
        return conn, share

    def _parse_csv(self, content: str, filename: str) -> List[Dict[str, Any]]:
        results = []
        try:
            reader  = csv.reader(io.StringIO(content))
            headers = None
            for raw_row in reader:
                row = [c.strip() for c in raw_row]
                if not any(row):
                    continue
                if headers is None:
                    joined = ",".join(row).lower()
                    if "parameter" in joined and ("result" in joined or "value1" in joined):
                        headers = row
                    continue
                d         = dict(zip(headers, row))
                parameter = d.get("Parameter") or d.get("parameter", "")
                if not parameter or parameter in ("Parameter", "DR6000", "DR3900"):
                    continue
                result_val = _parse_float(d.get("Result") or d.get("result", ""))
                if result_val is None:
                    continue
                unit     = (d.get("Unit") or d.get("unit") or "mg/L").strip()
                test_key = _safe_key(parameter)
                metrics: Dict[str, Any] = {test_key: {"value": result_val, "unit": unit}}
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
            logger.error("DR3900: parse_csv error in %s: %s", filename, e)
            results.append({"status": "error", "message": f"DR3900 parse error in {filename}: {e}"})
        return results


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None

def _safe_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")