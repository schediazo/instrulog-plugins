import asyncio
import struct
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class HachBiotectorTOCIntegration:

    @property
    def instrument_type(self) -> str:
        return "hach_biotector_b7000_modbus_tcp"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip   = config.get("ip",   "127.0.0.1")
        port = config.get("port", 5021)
        logger.info("HachBiotectorTOC: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)
        logger.info("HachBiotectorTOC: connected")
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if len(raw_bytes) < 19:
            logger.warning("HachBiotectorTOC: short packet (%d bytes)", len(raw_bytes))
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes (expected ≥19)"
            }

        try:
            transaction_id, protocol_id, length, unit_id = struct.unpack(
                ">HHHB", raw_bytes[:7]
            )
            function_code = raw_bytes[7]
            byte_count    = raw_bytes[8]

            if function_code != 0x03:
                return {
                    "status": "error",
                    "message": f"Unexpected function code: {function_code:#04x}"
                }

            toc_raw, tc_raw, tic_raw, tnb_raw, status_raw = struct.unpack(
                ">HHHHH", raw_bytes[9:19]
            )

            toc = round(toc_raw / 100.0, 2)
            tc  = round(tc_raw  / 100.0, 2)
            tic = round(tic_raw / 100.0, 2)
            tnb = round(tnb_raw / 100.0, 2)

            status_map = {0: "OK", 1: "Calibrating", 2: "Error"}
            status_str = status_map.get(status_raw, f"Unknown({status_raw})")

            if status_raw == 2:
                return {
                    "status": "error",
                    "message": f"Instrument reports error (status register={status_raw})"
                }

            logger.debug(
                "HachBiotectorTOC: TOC=%.2f TC=%.2f TIC=%.2f TNb=%.2f Status=%s",
                toc, tc, tic, tnb, status_str
            )

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "instrument_status": status_str,
                "metrics": {
                    "TOC": {
                        "value": toc,
                        "unit": "mg/L"
                    },
                    "TC": {
                        "value": tc,
                        "unit": "mg/L"
                    },
                    "TIC": {
                        "value": tic,
                        "unit": "mg/L"
                    },
                    "TNb": {
                        "value": tnb,
                        "unit": "mg/L"
                    }
                }
            }

        except struct.error as e:
            logger.error("HachBiotectorTOC: unpack error: %s | raw=%s", e, raw_bytes.hex())
            return {"status": "error", "message": f"Modbus parse error: {e}"}
        except Exception as e:
            logger.error("HachBiotectorTOC: unexpected error: %s", e)
            return {"status": "error", "message": f"Plugin error: {e}"}