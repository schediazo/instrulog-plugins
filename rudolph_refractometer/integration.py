"""
Rudolph J-Series Refractometer — TeraTerm Log Reader
=====================================================
Reads a single append-only TeraTerm log file containing repeated measurement
blocks separated by blank lines.  On every poll cycle the plugin finds the
last complete block and returns all its metrics.

HOST / IP field → directory containing the log file  (e.g. C:\\DR_DATA)
PORT field      → ignored

Block format:
    Mon Jul 6, 2026 08:13 AM

    J47HA Serial No. 17872

    Sample ID:
    Result   : 0.08 Brix
    N        : 1
    Temp     : 22.21 C
    T Corr.  : 20.00 C

Metrics returned:
    result      (Brix)
    n           (count)
    temperature (C)
    tempCorr    (C)
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

FILENAME = "teraterm.log"

# Field patterns — each captures the numeric value
_PATTERNS = {
    "result":      re.compile(r"^Result\s*:\s*([-\d.]+)\s*Brix", re.IGNORECASE),
    "n":           re.compile(r"^N\s*:\s*(\d+)"),
    "temperature": re.compile(r"^Temp\s*:\s*([-\d.]+)\s*C", re.IGNORECASE),
    "tempCorr":    re.compile(r"^T\s*Corr\.\s*:\s*([-\d.]+)\s*C", re.IGNORECASE),
}

_UNITS = {
    "result":      "Brix",
    "n":           "count",
    "temperature": "C",
    "tempCorr":    "C",
}


def _parse_last_block(filepath: str) -> Dict[str, Any] | None:
    """
    Read the file, split into blocks on blank lines, find the last block
    that contains at least a Result line, and parse all known fields.
    Returns a metrics dict or None if no valid block found.
    """
    with open(filepath, "r", encoding="ascii", errors="ignore") as f:
        content = f.read()

    # Split on sequences of blank lines
    raw_blocks = re.split(r"\n\s*\n", content)

    # Walk backwards to find the last block with a Result line
    last_metrics = None
    for block in reversed(raw_blocks):
        block = block.strip()
        if not block:
            continue

        metrics = {}
        for key, pattern in _PATTERNS.items():
            for line in block.splitlines():
                m = pattern.match(line.strip())
                if m:
                    try:
                        metrics[key] = float(m.group(1))
                    except ValueError:
                        pass
                    break

        if "result" in metrics:
            last_metrics = metrics
            break

    return last_metrics


class _FileReader:
    """Fake reader — sleeps poll_interval then returns a dummy byte."""
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


class RudolphJSeriesBrixIntegration:

    def __init__(self) -> None:
        self._log_file: str = ""

    @property
    def instrument_type(self) -> str:
        return "rudolph_jseries_brix_teraterm"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        directory = config.get("ip", "").strip()
        poll_seconds = config.get("poll_interval_ms", 2000) / 1000.0

        if not directory:
            raise ValueError("Rudolph J-Series: HOST/IP must be the log directory path")

        log_file = os.path.join(directory, FILENAME)
        if not os.path.isfile(log_file):
            raise FileNotFoundError(f"Rudolph J-Series: file not found: {log_file!r}")

        self._log_file = log_file
        logger.info("Rudolph J-Series plugin: tailing %r", self._log_file)
        return _FileReader(poll_seconds), _NullWriter()

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if not self._log_file:
            return {"status": "error", "message": "Not connected"}

        try:
            metrics = await asyncio.to_thread(_parse_last_block, self._log_file)

            if metrics is None:
                return {"status": "error", "message": "No valid measurement block found in log"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    key: {"value": val, "unit": _UNITS[key]}
                    for key, val in metrics.items()
                },
            }

        except Exception as e:
            logger.error("Rudolph J-Series plugin error: %s", e)
            return {"status": "error", "message": f"Rudolph J-Series plugin error: {e}"}
