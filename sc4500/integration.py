"""
Hach SC4500 pH Meter — Modbus TCP Plugin
=========================================
Reads pH and temperature via Modbus TCP function code 03
(Read Holding Registers).

The SC4500 Telegram exposes each sensor tag as an IEEE 754 float
spanning 2 consecutive holding registers (4 bytes).  Default byte
order is big-endian / "Normal" (no swap).

Telegram layout (configured on the device):
    Tag 0  → registers 0–1  → pH        [pH]
    Tag 1  → registers 2–3  → Temperature [°C]

HOST / IP field → instrument IP address
PORT field      → 502 (Modbus TCP default)

Modbus request: FC03, start register 0, quantity 4 (reads both tags
in one request → 8 bytes payload).
"""

import asyncio
import logging
import struct
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ── Modbus TCP constants ──────────────────────────────────────────────────────
MODBUS_PORT      = 502
UNIT_ID          = 1
FC_READ_HOLDING  = 0x03
START_REGISTER   = 0       # first register of Tag 0
REGISTER_COUNT   = 4       # 2 registers × 2 tags

# Modbus TCP ADU: transaction_id(2) + protocol_id(2) + length(2) + unit_id(1)
#                 + fc(1) + start_reg(2) + reg_count(2)
def _build_request(transaction_id: int = 1) -> bytes:
    pdu    = struct.pack(">BBHH", UNIT_ID, FC_READ_HOLDING, START_REGISTER, REGISTER_COUNT)
    header = struct.pack(">HHH", transaction_id, 0, len(pdu))
    return header + pdu


def _parse_response(data: bytes) -> Dict[str, float]:
    """
    Parse a Modbus TCP FC03 response.
    Expected structure:
        [0:2]  transaction id
        [2:4]  protocol id (0x0000)
        [4:6]  length
        [6]    unit id
        [7]    function code (0x03)
        [8]    byte count  (should be 8 for 4 registers)
        [9:13] Tag 0 float (pH)
        [13:17] Tag 1 float (temperature)
    """
    if len(data) < 8:
        raise ValueError(f"Response too short: {len(data)} bytes")

    fc = data[7]
    if fc & 0x80:
        exc = data[8] if len(data) > 8 else 0xFF
        raise ValueError(f"Modbus exception code: {exc:#04x}")

    if len(data) < 17:
        raise ValueError(f"Response too short for FC03: {len(data)} bytes (need 17)")
    if fc != FC_READ_HOLDING:
        raise ValueError(f"Unexpected function code: {fc:#04x}")

    byte_count = data[8]
    if byte_count < 8:
        raise ValueError(f"Not enough register data: byte_count={byte_count}")

    ph_val   = struct.unpack_from(">f", data, 9)[0]
    temp_val = struct.unpack_from(">f", data, 13)[0]
    return {"ph": ph_val, "temperature": temp_val}


class HachSC4500PhIntegration:

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = MODBUS_PORT
        self._transaction_id: int = 0

    @property
    def instrument_type(self) -> str:
        return "hach_sc4500_ph"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._host = config.get("ip", "").strip()
        self._port = int(config.get("port") or MODBUS_PORT)

        if not self._host:
            raise ValueError("SC4500: HOST/IP must be the instrument IP address")

        reader, writer = await asyncio.open_connection(self._host, self._port)
        logger.info("SC4500: connected to %s:%s", self._host, self._port)
        return reader, writer

    def build_request(self) -> bytes:
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        return _build_request(self._transaction_id)

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        try:
            if not raw_bytes:
                return {"status": "error", "message": "Empty response from SC4500"}

            values = _parse_response(raw_bytes)

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    "ph":          {"value": round(values["ph"],   3), "unit": "pH"},
                    "temperature": {"value": round(values["temperature"], 2), "unit": "C"},
                },
            }

        except Exception as e:
            logger.error("SC4500 parse error: %s", e)
            return {"status": "error", "message": f"SC4500 parse error: {e}"}
