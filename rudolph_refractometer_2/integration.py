"""
Rudolph J-Series Refractometer 2 — Serial (RS-232)
===================================================
Reads directly from the instrument's RS-232 serial port.
No TeraTerm or log file needed.

The instrument continuously outputs one tab-separated line per measurement:

    2026.08.03\t22:22:03\t\t26.3\tBrix\t42.57\r\n
    col0: date
    col1: time
    col2: sample ID (may be empty)
    col3: temperature (°C)
    col4: unit label ("Brix")
    col5: result value (Brix)

Config keys:
  ip (Serial Port)  — COM4  or  /dev/ttyUSB1
  baud_rate         — 9600
  bytesize          — 8
  parity            — N
  stopbits          — 1

Metrics returned:
  temperature  (C)
  result       (Brix)
"""

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_MAX_BUF = 2048


class RudolphJSeriesRefractometer2Integration:

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer: str = ""

    @property
    def instrument_type(self) -> str:
        return "rudolph_jseries_refractometer2_serial"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        import serial_asyncio

        port     = config.get("ip", "").strip()
        baud     = int(config.get("baud_rate",  9600))
        bytesize = int(config.get("bytesize",   8))
        parity   =     config.get("parity",    "N")
        stopbits = int(config.get("stopbits",   1))

        if not port:
            raise ValueError("Refractometer 2: serial port not configured (set Host/IP to COM4 or /dev/ttyUSB1)")

        logger.info("Refractometer 2: opening %s @ %d baud", port, baud)
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=port,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
        )
        logger.info("Refractometer 2: serial port open")
        return self._reader, self._writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        try:
            self._buffer += raw_bytes.decode("ascii", errors="ignore")

            if len(self._buffer) > _MAX_BUF:
                self._buffer = self._buffer[-_MAX_BUF:]

            values = _parse_last_line(self._buffer)

            if values is None:
                return {"status": "error", "message": "No complete measurement line yet"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    "temperature": {"value": values["temperature"], "unit": "C"},
                    "result":      {"value": values["result"],      "unit": "Brix"},
                },
            }

        except Exception as e:
            logger.error("Refractometer 2 serial error: %s", e)
            return {"status": "error", "message": f"Refractometer 2 error: {e}"}


def _parse_last_line(text: str) -> Dict[str, float] | None:
    """Find the last complete (newline-terminated) TSV line and parse it."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None

    # Walk backwards to find a parseable line
    for line in reversed(lines):
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) < 6:
            continue
        try:
            return {
                "temperature": float(cols[3]),
                "result":      float(cols[5]),
            }
        except (ValueError, IndexError):
            continue

    return None