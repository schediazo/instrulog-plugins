"""
Weight / Balance — Serial (RS-232)
===================================
Reads directly from a balance or scale via RS-232 serial port.
No TeraTerm or log file needed.

Most balances continuously stream one line per stable reading, e.g.:
    "     0.9 g\r\n"     — with unit
    "     0.9\r\n"       — value only
    "ST,GS,+    0.123g\r\n"  — Mettler Toledo stable/gross format
    "     -0.2 g\r\n"    — negative (tare)

This plugin extracts the last parseable float from the incoming stream.

Config keys:
  ip (Serial Port)  — COM5  or  /dev/ttyUSB2
  baud_rate         — 9600  (common default; some balances use 4800 or 19200)
  bytesize          — 8
  parity            — N
  stopbits          — 1
  metric            — metric key name  (default: "weight")
  unit              — unit string      (default: "g")
"""

import asyncio
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

_MAX_BUF = 1024

# Matches an optional sign, digits, optional decimal — anywhere in a line
_FLOAT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)")


class WeightIntegration:

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer: str = ""
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "weight_scale_serial"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        import serial_asyncio

        port     = config.get("ip", "").strip()
        baud     = int(config.get("baud_rate",  9600))
        bytesize = int(config.get("bytesize",   8))
        parity   =     config.get("parity",    "N")
        stopbits = int(config.get("stopbits",   1))
        self._config = config

        if not port:
            raise ValueError("Weight: serial port not configured (set Host/IP to COM5 or /dev/ttyUSB2)")

        logger.info("Weight: opening %s @ %d baud", port, baud)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=port,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
        )
        logger.info("Weight: serial port open")
        return self._reader, self._writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        try:
            self._buffer += raw_bytes.decode("ascii", errors="ignore")

            if len(self._buffer) > _MAX_BUF:
                self._buffer = self._buffer[-_MAX_BUF:]

            value = _parse_last_value(self._buffer)

            if value is None:
                return {"status": "error", "message": "No parseable weight value yet"}

            metric = self._config.get("metric", "weight")
            unit   = self._config.get("unit",   "g")

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    metric: {"value": value, "unit": unit},
                },
            }

        except Exception as e:
            logger.error("Weight serial error: %s", e)
            return {"status": "error", "message": f"Weight error: {e}"}


def _parse_last_value(text: str) -> float | None:
    """
    Walk lines in reverse, return the first parseable float found.
    Skips lines that look like headers or status codes (no digits at all).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines):
        # Skip Mettler-Toledo status prefix lines that have no numeric content
        m = _FLOAT_RE.search(line)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None