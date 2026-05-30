"""
InstruLog Plugin: Rudolph J357 Automatic Refractometer (J-Series)
Connection:       Modbus TCP
Default Port:     5024

Register Map (FC 0x03):
  Reg 0–1 — Refractive Index nD (2×uint16, IEEE 754 float, dimensionless)
  Reg 2–3 — Brix               (2×uint16, IEEE 754 float, %Brix)
  Reg 4   — Temperature        (uint16, /100, °C)
  Reg 5   — Wavelength         (uint16, nm — typically 589 for Na D-line)
  Reg 6   — Status             (uint16, 0=OK 1=Measuring 2=BubbleDetected 3=Error)

Note: Refractive Index and Brix use 32-bit IEEE 754 floats packed
      across two consecutive big-endian uint16 Modbus registers.
"""

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

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip   = config.get("ip",   "127.0.0.1")
        port = config.get("port", 5024)
        logger.info("RudolphJSeries: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)
        logger.info("RudolphJSeries: connected")
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        # Minimum: 7 MBAP + 1 FC + 1 ByteCount + 14 data bytes = 23
        if len(raw_bytes) < 23:
            logger.warning("RudolphJSeries: short packet (%d bytes)", len(raw_bytes))
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes (expected ≥23)"
            }

        try:
            # MBAP header
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

            # Registers 0–1: Refractive Index nD (32-bit IEEE 754 float)
            ri_hi, ri_lo = struct.unpack(">HH", raw_bytes[9:13])
            ri = struct.unpack(">f", struct.pack(">HH", ri_hi, ri_lo))[0]

            # Registers 2–3: Brix (32-bit IEEE 754 float)
            brix_hi, brix_lo = struct.unpack(">HH", raw_bytes[13:17])
            brix = struct.unpack(">f", struct.pack(">HH", brix_hi, brix_lo))[0]

            # Register 4: Temperature (uint16, /100)
            temp_raw, = struct.unpack(">H", raw_bytes[17:19])
            temperature = round(temp_raw / 100.0, 2)

            # Register 5: Wavelength (uint16, nm)
            wavelength, = struct.unpack(">H", raw_bytes[19:21])

            # Register 6: Status
            status_raw, = struct.unpack(">H", raw_bytes[21:23])
            status_str = STATUS_MAP.get(status_raw, f"Unknown({status_raw})")

            # Round to realistic precision
            ri   = round(float(ri),   6)
            brix = round(float(brix), 4)

            # Bubble detection — instrument-flagged bad reading
            if status_raw == 2:
                return {
                    "status": "error",
                    "message": "Bubble detected in sample — reading discarded. "
                               "Check sample flow and degassing."
                }

            # Instrument error state
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
