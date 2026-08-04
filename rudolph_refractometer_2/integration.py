"""
Rudolph J-Series Refractometer — TSV Log Reader
================================================
Reads a single append-only TeraTerm log file where each measurement is one
tab-separated line appended continuously.

HOST / IP field → directory containing teraterm.log  (e.g. C:\\DR_DATA)
PORT field      → ignored

Line format (tab-separated):
    2026.08.03  22:22:03      26.3    Brix    42.57
    col0: date
    col1: time
    col2: (empty / sample ID)
    col3: temperature value
    col4: unit label  ("Brix")
    col5: result value (Brix)

Metrics returned:
    temperature  (C)
    result       (Brix)
"""

import asyncio
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

FILENAME = "teraterm.log"


def _parse_last_line(filepath: str) -> Dict[str, float] | None:
    """Return parsed fields from the last non-empty data line."""
    with open(filepath, "r", encoding="ascii", errors="ignore") as f:
        last = None
        for line in f:
            stripped = line.strip()
            if stripped:
                last = stripped

    if last is None:
        return None

    cols = [c.strip() for c in last.split("\t")]
    # Need at least 6 columns
    if len(cols) < 6:
        logger.warning("Refractometer: unexpected line format: %r", last)
        return None

    try:
        return {
            "temperature": float(cols[3]),
            "result":      float(cols[5]),
        }
    except (ValueError, IndexError) as e:
        logger.warning("Refractometer: cannot parse line %r: %s", last, e)
        return None


class _FileReader:
    def __init__(self, poll_seconds: float = 2.0) -> None:
        self._poll_seconds = poll_seconds

    async def read(self, n: int = -1) -> bytes:
        await asyncio.sleep(self._poll_seconds)
        return b"\x00"

    def at_eof(self) -> bool:
        return False


class _NullWriter:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


class RudolphJSeriesRefractometer2Integration:

    def __init__(self) -> None:
        self._log_file: str = ""

    @property
    def instrument_type(self) -> str:
        return "rudolph_jseries_refractometer2_teraterm"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        directory = config.get("ip", "").strip()
        poll_seconds = config.get("poll_interval_ms", 2000) / 1000.0

        if not directory:
            raise ValueError("Refractometer: HOST/IP must be the log directory path")

        log_file = os.path.join(directory, FILENAME)
        if not os.path.isfile(log_file):
            raise FileNotFoundError(f"Refractometer: file not found: {log_file!r}")

        self._log_file = log_file
        logger.info("Refractometer plugin: tailing %r", self._log_file)
        return _FileReader(poll_seconds), _NullWriter()

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if not self._log_file:
            return {"status": "error", "message": "Not connected"}

        try:
            values = await asyncio.to_thread(_parse_last_line, self._log_file)

            if values is None:
                return {"status": "error", "message": "No valid data line found in log"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    "temperature": {"value": values["temperature"], "unit": "C"},
                    "result":      {"value": values["result"],      "unit": "Brix"},
                },
            }

        except Exception as e:
            logger.error("Refractometer plugin error: %s", e)
            return {"status": "error", "message": f"Refractometer plugin error: {e}"}
