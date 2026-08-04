"""
TeraTerm Log Reader
===================
Protocol  : File-based (no TCP connection)
Log format: One float per line, space-padded, appended continuously, e.g.:
               0.9
               0.3
              -0.2

On every poll cycle the plugin seeks to EOF, walks back to find the last
non-empty line, and returns it as a single metric.  The file is never fully
re-read regardless of how large it grows.

Config keys (set on the instrument record in InstruLog):
  log_file  – absolute path to the TeraTerm log file  (required)
  metric    – metric key name                          (default: "reading")
  unit      – unit string for the frontend column      (default: "units")
"""

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _read_last_value(filepath: str) -> float | None:
    """
    Seek to EOF and scan backwards for the last non-empty line.
    Returns the parsed float, or None if the file is empty/unparseable.
    Never reads the whole file.
    """
    with open(filepath, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return None

        # Skip trailing whitespace / newlines
        pos = size - 1
        while pos >= 0:
            f.seek(pos)
            ch = f.read(1)
            if ch not in (b"\n", b"\r", b" "):
                break
            pos -= 1

        if pos < 0:
            return None

        # Scan left to start of this line
        while pos > 0:
            f.seek(pos - 1)
            ch = f.read(1)
            if ch in (b"\n", b"\r"):
                break
            pos -= 1

        f.seek(pos)
        line = f.readline().decode("ascii", errors="ignore").strip()

    if not line:
        return None

    try:
        return float(line)
    except ValueError:
        logger.warning("TeraTerm: could not parse %r as float", line)
        return None


class TeratemLogIntegration:

    def __init__(self) -> None:
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "teraterm_log_file"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        log_file = config.get("log_file", "")

        if not log_file:
            raise ValueError("TeraTerm plugin: 'log_file' must be set in instrument config")
        if not os.path.isfile(log_file):
            raise FileNotFoundError(f"TeraTerm plugin: file not found: {log_file!r}")

        logger.info("TeraTerm plugin: tailing %r", log_file)
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        log_file = self._config.get("log_file", "")
        metric   = self._config.get("metric", "reading")
        unit     = self._config.get("unit",   "units")

        if not log_file:
            return {"status": "error", "message": "log_file not configured"}

        try:
            value = await asyncio.to_thread(_read_last_value, log_file)

            if value is None:
                return {"status": "error", "message": f"Log file is empty: {log_file!r}"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    metric: {
                        "value": value,
                        "unit":  unit,
                    }
                },
            }

        except Exception as e:
            logger.error("TeraTerm plugin error: %s", e)
            return {"status": "error", "message": f"TeraTerm plugin error: {e}"}
