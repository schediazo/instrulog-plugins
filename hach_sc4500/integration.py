# plugins/hach_sc4500_modbus/integration.py
"""
HACH SC4500 Controller (pH / Conductivity / DO) — Modbus TCP over LAN Plugin
Connectivity: Modbus, 4-20mA, RS232, RS485, LAN

The SC4500 supports Modbus TCP natively over its LAN port (default port 502).
This plugin reads holding registers for pH, conductivity, dissolved oxygen,
and temperature from the SC4500 Modbus register map.

Config keys:
  ip    — SC4500 IP address
  port  — Modbus TCP port (default: 502)

SC4500 Modbus Register Map (Holding Registers, Function Code 0x03):
  Reg 0x0001 (1)  : pH value           — float32, 2 registers, scale ×1
  Reg 0x0003 (3)  : Temperature (pH)   — float32, 2 registers, unit °C
  Reg 0x0005 (5)  : Conductivity       — float32, 2 registers, unit µS/cm
  Reg 0x0007 (7)  : Temperature (Cond) — float32, 2 registers, unit °C
  Reg 0x0009 (9)  : DO                 — float32, 2 registers, unit mg/L
  Reg 0x000B (11) : Temperature (DO)   — float32, 2 registers, unit °C

Note: SC4500 stores float32 values in two consecutive 16-bit registers (big-endian word order).
The plugin reads 12 registers starting at address 1 in a single request.
"""

import asyncio
import struct
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Modbus request: read 12 holding registers starting at address 0x0001
# MBAP header: Transaction ID (2B) | Protocol ID (2B, 0x0000) | Length (2B) | Unit ID (1B)
# PDU:         Function Code (1B, 0x03) | Start Address (2B) | Quantity (2B)
_UNIT_ID     = 1
_START_ADDR  = 0x0001
_REG_COUNT   = 12  # 6 float32 values × 2 registers each


def _build_request(transaction_id: int) -> bytes:
    mbap = struct.pack(">HHHB", transaction_id, 0x0000, 6, _UNIT_ID)
    pdu  = struct.pack(">BHH", 0x03, _START_ADDR, _REG_COUNT)
    return mbap + pdu


class HachSC4500ModbusIntegration:

    def __init__(self):
        self._transaction_id = 0
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "hach_sc4500_modbus_tcp"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        ip   = config.get("ip",   "192.168.1.102")
        port = config.get("port", 502)
        logger.info("SC4500: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)

        # Send initial request — stream engine will call parse_to_json with the response
        self._transaction_id = 1
        writer.write(_build_request(self._transaction_id))
        await writer.drain()

        logger.info("SC4500: connected, initial request sent")
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        # Minimum valid Modbus TCP response for 12 registers:
        # 7 (MBAP) + 1 (FC) + 1 (byte count) + 24 (12 registers × 2 bytes) = 33 bytes
        MIN_LEN = 33

        if len(raw_bytes) < MIN_LEN:
            return {
                "status": "error",
                "message": f"SC4500: short packet {len(raw_bytes)}B (need {MIN_LEN})",
            }

        try:
            # MBAP header
            transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", raw_bytes[:7])
            function_code = raw_bytes[7]
            byte_count    = raw_bytes[8]

            # Check for Modbus exception response (FC | 0x80)
            if function_code & 0x80:
                exception_code = raw_bytes[8] if len(raw_bytes) > 8 else 0
                return {
                    "status": "error",
                    "message": f"SC4500: Modbus exception FC={function_code:#04x} code={exception_code}",
                }

            if function_code != 0x03:
                return {
                    "status": "error",
                    "message": f"SC4500: unexpected function code {function_code:#04x}",
                }

            # Register data starts at byte 9
            reg_data = raw_bytes[9: 9 + byte_count]
            if len(reg_data) < _REG_COUNT * 2:
                return {
                    "status": "error",
                    "message": f"SC4500: insufficient register data ({len(reg_data)}B)",
                }

            # Unpack 6 float32 values (big-endian, 2 registers each)
            # Register pairs: (0,1)=pH, (2,3)=temp_pH, (4,5)=cond, (6,7)=temp_cond, (8,9)=DO, (10,11)=temp_DO
            floats = struct.unpack(">ffffff", reg_data[:24])
            ph_val, temp_ph, cond_val, temp_cond, do_val, temp_do = floats

            # Use pH temperature as the primary temperature (probes share one value on most configs)
            temperature = round(temp_ph, 2)

            metrics: Dict[str, Any] = {
                "pH":              {"value": round(ph_val,   3), "unit": "pH"},
                "conductivity":    {"value": round(cond_val, 2), "unit": "µS/cm"},
                "dissolvedOxygen": {"value": round(do_val,   3), "unit": "mg/L"},
                "temperature":     {"value": temperature,        "unit": "Celsius"},
            }

            # Append per-probe temperatures if they differ significantly
            if abs(temp_cond - temp_ph) > 0.5:
                metrics["temperatureConductivity"] = {"value": round(temp_cond, 2), "unit": "Celsius"}
            if abs(temp_do - temp_ph) > 0.5:
                metrics["temperatureDO"]           = {"value": round(temp_do, 2),   "unit": "Celsius"}

            logger.debug(
                "SC4500 parsed: pH=%.3f cond=%.2f DO=%.3f temp=%.2f",
                ph_val, cond_val, do_val, temperature,
            )

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "metrics": metrics,
            }

        except struct.error as e:
            logger.error("SC4500: struct unpack failed: %s | raw=%s", e, raw_bytes.hex())
            return {"status": "error", "message": f"SC4500 Modbus parse error: {e}"}
        except Exception as e:
            logger.exception("SC4500: unexpected error")
            return {"status": "error", "message": f"SC4500 plugin error: {e}"}
