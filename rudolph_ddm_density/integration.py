import asyncio
import struct
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

STATUS_MAP = {
    0: "OK",
    1: "Measuring",
    2: "Error",
    3: "Calibrating",
}


class RudolphDDMDensityIntegration:

    @property
    def instrument_type(self) -> str:
        return "rudolph_ddm2911_modbus_tcp"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip   = config.get("ip",   "127.0.0.1")
        port = config.get("port", 5023)
        logger.info("RudolphDDM: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)
        logger.info("RudolphDDM: connected")
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if len(raw_bytes) < 21:
            logger.warning("RudolphDDM: short packet (%d bytes)", len(raw_bytes))
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes (expected >=21)"
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

            d_hi, d_lo = struct.unpack(">HH", raw_bytes[9:13])
            density = struct.unpack(">f", struct.pack(">HH", d_hi, d_lo))[0]

            c_hi, c_lo = struct.unpack(">HH", raw_bytes[13:17])
            concentration = struct.unpack(">f", struct.pack(">HH", c_hi, c_lo))[0]

            temp_raw, = struct.unpack(">H", raw_bytes[17:19])
            temperature = round(temp_raw / 100.0, 2)

            status_raw, = struct.unpack(">H", raw_bytes[19:21])
            status_str = STATUS_MAP.get(status_raw, f"Unknown({status_raw})")

            density       = round(float(density), 6)
            concentration = round(float(concentration), 4)

            if status_raw == 2:
                return {
                    "status": "error",
                    "message": f"Density meter reports error (status={status_raw})"
                }

            logger.debug(
                "RudolphDDM: density=%.6f conc=%.4f temp=%.2f status=%s",
                density, concentration, temperature, status_str
            )

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "instrument_status": status_str,
                "metrics": {
                    "density": {
                        "value": density,
                        "unit": "g/cm³"
                    },
                    "concentration": {
                        "value": concentration,
                        "unit": "%w/w"
                    },
                    "temperature": {
                        "value": temperature,
                        "unit": "Celsius"
                    }
                }
            }

        except struct.error as e:
            logger.error("RudolphDDM: unpack error: %s | raw=%s", e, raw_bytes.hex())
            return {"status": "error", "message": f"Modbus parse error: {e}"}
        except Exception as e:
            logger.error("RudolphDDM: unexpected error: %s", e)
            return {"status": "error", "message": f"Plugin error: {e}"}
