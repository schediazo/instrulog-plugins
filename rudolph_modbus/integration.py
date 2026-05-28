import asyncio
import struct
from typing import Dict, Any


class RudolphModbusIntegration:

    @property
    def instrument_type(self) -> str:
        return "rudolph_modbus_tcp"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip = config.get("ip", "127.0.0.1")
        port = config.get("port", 5020)
        reader, writer = await asyncio.open_connection(ip, port)
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if len(raw_bytes) < 13:
            return {"status": "error", "message": "Incomplete Modbus packet"}

        header = struct.unpack(">HHHB", raw_bytes[:7])
        transaction_id = header[0]
        function_code = raw_bytes[7]
        byte_count = raw_bytes[8]

        if function_code != 3:
            return {"status": "error", "message": f"Unsupported function code: {function_code}"}

        ph_raw, temp_raw = struct.unpack(">HH", raw_bytes[9:13])
        return {
            "status": "success",
            "instrument_type": self.instrument_type,
            "transaction_id": transaction_id,
            "metrics": {
                "pH": {"value": ph_raw / 100.0, "unit": "pH"},
                "temperature": {"value": temp_raw / 100.0, "unit": "Celsius"},
            }
        }
