import asyncio
import struct
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

STATUS_MAP = {
    0: "OK",
    1: "Measuring",
    2: "BubbleDetected",
    3: "Error",
}


class RudolphJSeriesRefractometerIntegration:

    @property
    def instrument_type(self) -> str:
        return "rudolph_j357_modbus_tcp"
        
    def build_request(self, transaction_id: int = 1) -> bytes:
        """Modbus TCP Read Holding Registers — 8 registers from address 0."""
        unit_id       = 0x01
        function_code = 0x03
        start_address = 0x0000
        num_registers = 0x0008
        pdu  = struct.pack(">BBHH", unit_id, function_code, start_address, num_registers)
        mbap = struct.pack(">HHH",  transaction_id & 0xFFFF, 0x0000, len(pdu) + 1)
        return mbap + pdu

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip   = config.get("ip",   "127.0.0.1")
        port = config.get("port", 5024)
        logger.info("RudolphJSeries: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)
        logger.info("RudolphJSeries: connected")
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        if len(raw_bytes) < 23:
            logger.warning("RudolphJSeries: short packet (%d bytes)", len(raw_bytes))
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes (expected >=23)"
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

            ri_hi, ri_lo = struct.unpack(">HH", raw_bytes[9:13])
            ri = struct.unpack(">f", struct.pack(">HH", ri_hi, ri_lo))[0]

            brix_hi, brix_lo = struct.unpack(">HH", raw_bytes[13:17])
            brix = struct.unpack(">f", struct.pack(">HH", brix_hi, brix_lo))[0]

            temp_raw, = struct.unpack(">H", raw_bytes[17:19])
            temperature = round(temp_raw / 100.0, 2)

            wavelength, = struct.unpack(">H", raw_bytes[19:21])

            status_raw, = struct.unpack(">H", raw_bytes[21:23])
            status_str = STATUS_MAP.get(status_raw, f"Unknown({status_raw})")

            ri   = round(float(ri),   6)
            brix = round(float(brix), 4)

            if status_raw == 2:
                return {
                    "status": "error",
                    "message": "Bubble detected in sample — reading discarded. Check sample flow and degassing."
                }

            if status_raw == 3:
                return {
                    "status": "error",
                    "message": f"Refractometer reports hardware error (status={status_raw})"
                }

            logger.debug(
                "RudolphJSeries: nD=%.6f Brix=%.4f temp=%.2f λ=%dnm status=%s",
                ri, brix, temperature, wavelength, status_str
            )

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "instrument_status": status_str,
                "metrics": {
                    "refractiveIndex": {
                        "value": ri,
                        "unit": "nD"
                    },
                    "brix": {
                        "value": brix,
                        "unit": "%Brix"
                    },
                    "temperature": {
                        "value": temperature,
                        "unit": "Celsius"
                    },
                    "wavelength": {
                        "value": wavelength,
                        "unit": "nm"
                    }
                }
            }

        except struct.error as e:
            logger.error("RudolphJSeries: unpack error: %s | raw=%s", e, raw_bytes.hex())
            return {"status": "error", "message": f"Modbus parse error: {e}"}
        except Exception as e:
            logger.error("RudolphJSeries: unexpected error: %s", e)
            return {"status": "error", "message": f"Plugin error: {e}"}
