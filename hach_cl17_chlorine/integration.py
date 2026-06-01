import asyncio
import struct
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

ALARM_MAP = {
    0: "None",
    1: "LowReagent",
    2: "HighChlorine",
    3: "LowChlorine",
}


class HachCL17ChlorineIntegration:

    @property
    def instrument_type(self) -> str:
        return "hach_cl17sc_modbus_tcp"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip   = config.get("ip",   "127.0.0.1")
        port = config.get("port", 5022)
        logger.info("HachCL17: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)
        logger.info("HachCL17: connected")
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if len(raw_bytes) < 19:
            logger.warning("HachCL17: short packet (%d bytes)", len(raw_bytes))
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes (expected >=19)"
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

            free_raw, total_raw, temp_raw, reagent_raw, alarm_raw = struct.unpack(
                ">HHHHH", raw_bytes[9:19]
            )

            free_cl  = round(free_raw   / 100.0, 2)
            total_cl = round(total_raw  / 100.0, 2)
            temp     = round(temp_raw   / 100.0, 2)
            reagent  = round(reagent_raw / 10.0,  1)
            alarm    = ALARM_MAP.get(alarm_raw, f"Unknown({alarm_raw})")

            logger.debug(
                "HachCL17: FreeCl=%.2f TotalCl=%.2f Temp=%.2f Reagent=%.1f Alarm=%s",
                free_cl, total_cl, temp, reagent, alarm
            )

            reading_status = "success"
            error_message  = None
            if alarm_raw == 1 and reagent < 10.0:
                reading_status = "error"
                error_message  = f"Reagent critically low: {reagent}% replace reagent cartridge"

            result: Dict[str, Any] = {
                "status": reading_status,
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "alarm": alarm,
                "metrics": {
                    "freeChlorine": {
                        "value": free_cl,
                        "unit": "mg/L"
                    },
                    "totalChlorine": {
                        "value": total_cl,
                        "unit": "mg/L"
                    },
                    "temperature": {
                        "value": temp,
                        "unit": "Celsius"
                    },
                    "reagentLevel": {
                        "value": reagent,
                        "unit": "%"
                    }
                }
            }

            if error_message:
                result["message"] = error_message

            return result

        except struct.error as e:
            logger.error("HachCL17: unpack error: %s | raw=%s", e, raw_bytes.hex())
            return {"status": "error", "message": f"Modbus parse error: {e}"}
        except Exception as e:
            logger.error("HachCL17: unexpected error: %s", e)
            return {"status": "error", "message": f"Plugin error: {e}"}
