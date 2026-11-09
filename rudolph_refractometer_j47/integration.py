"""
Rudolph J47-Series Refractometer — Serial (RS-232)
=================================================
Reads directly from the instrument's RS-232 serial port.
No TeraTerm or log file needed.

The instrument sends one measurement block per reading cycle, terminated
by a blank line. Block format (same as TeraTerm output):

    Mon Jul 6, 2026 08:13 AM\r\n
    \r\n
    J47HA Serial No. 17872\r\n
    \r\n
    Sample ID:\r\n
    Result   : 0.08 Brix\r\n
    N        : 1\r\n
    Temp     : 22.21 C\r\n
    T Corr.  : 20.00 C\r\n
    \r\n

Config keys (set on the instrument record in InstruLog):
  ip (Serial Port)  — COM3  or  /dev/ttyUSB0
  baud_rate         — 9600  (Rudolph default)
  bytesize          — 8
  parity            — N
  stopbits          — 1
  poll_interval_ms  — controls how often we request / read; 2000 is fine

Metrics returned:
  result       (Brix)
  n            (count)
  temperature  (C)
  tempCorr     (C)
"""

import asyncio
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Field patterns — same as before, instrument output hasn't changed
_PATTERNS = {
    "result":      re.compile(r"^Result\s*:\s*([-\d.]+)\s*Brix",  re.IGNORECASE),
    "n":           re.compile(r"^N\s*:\s*(\d+)"),
    "temperature": re.compile(r"^Temp\s*:\s*([-\d.]+)\s*C",       re.IGNORECASE),
    "tempCorr":    re.compile(r"^T\s*Corr\.\s*:\s*([-\d.]+)\s*C", re.IGNORECASE),
}

_UNITS = {
    "result":      "Brix",
    "n":           "count",
    "temperature": "C",
    "tempCorr":    "C",
}

# Max bytes to buffer before we force a parse attempt
_MAX_BUF = 4096


class RudolphJ47SeriesRefractometerIntegration:

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer: str = ""
        self._poll_seconds: float = 2.0

    @property
    def instrument_type(self) -> str:
        return "rudolph_jseries_brix_serial"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        import serial_asyncio  # pyserial-asyncio

        port     = config.get("ip", "").strip() 
        baud     = int(config.get("baud_rate",  9600))
        bytesize = int(config.get("bytesize",   8))
        parity   =     config.get("parity",    "N")
        stopbits = int(config.get("stopbits",   1))
        self._poll_seconds = config.get("poll_interval_ms", 2000) / 1000.0

        if not port:
            raise ValueError("Rudolph Refractometer: serial port not configured (set Host/IP to COM3 or /dev/ttyUSB0)")

        logger.info("Rudolph Refractometer: opening %s @ %d baud", port, baud)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=port,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
        )
        logger.info("Rudolph Refractometer: serial port open")
        return self._reader, self._writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        """
        stream_engine calls this with whatever bytes were read in this poll cycle.
        We accumulate into a buffer and parse the last complete block.
        """
        try:
            self._buffer += raw_bytes.decode("ascii", errors="ignore")

            # Keep buffer bounded
            if len(self._buffer) > _MAX_BUF:
                self._buffer = self._buffer[-_MAX_BUF:]

            metrics = _parse_last_block(self._buffer)

            if metrics is None:
                return {"status": "error", "message": "No complete measurement block yet"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    key: {"value": val, "unit": _UNITS[key]}
                    for key, val in metrics.items()
                },
            }

        except Exception as e:
            logger.error("Rudolph Refractometer serial error: %s", e)
            return {"status": "error", "message": f"Rudolph Refractometer error: {e}"}


def _parse_last_block(text: str) -> Dict[str, float] | None:
    """Split buffered text into blocks on blank lines, parse the last valid one."""
    raw_blocks = re.split(r"\n\s*\n", text)

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
            return metrics

    return None