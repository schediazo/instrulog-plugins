# plugins/rudolph_apiz_polarimeter/integration.py
"""
Rudolph Research API-Z Polarimeter — LAN Plugin (REST / HTTP)
Connectivity: USB, RS232, Printer, LAN

The API-Z exposes an HTTP endpoint on its LAN port for data retrieval.
This plugin polls the REST endpoint at the configured IP/port.

Config keys:
  ip    — instrument IP address
  port  — HTTP port (default: 80)

Metrics reported:
  opticalRotation  (degrees, °)
  temperature      (Celsius)
  concentration    (%, optional — if configured on instrument)
  wavelength       (nm, typically 589 for Na-D line)

Rudolph API-Z HTTP response (typical JSON):
{
  "status": "OK",
  "opticalRotation": -0.345,
  "temperature": 20.02,
  "concentration": 12.5,
  "wavelength": 589,
  "unit": "%"
}
"""

import asyncio
import aiohttp
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

TIMEOUT_SEC = 10


class RudolphApiZPolarimeterIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "rudolph_apiz_polarimeter_rest"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        ip   = self._config.get("ip", "192.168.1.100")
        port = self._config.get("port", 80)
        url  = f"http://{ip}:{port}/api/measurement/latest"

        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {
                            "status": "error",
                            "message": f"API-Z HTTP {resp.status} from {url}",
                        }
                    data = await resp.json(content_type=None)

            # Check instrument-level status if present
            instr_status = str(data.get("status", "OK")).upper()
            if instr_status not in ("OK", "READY", "SUCCESS", ""):
                return {
                    "status": "error",
                    "message": f"API-Z instrument status: {instr_status}",
                }

            metrics: Dict[str, Any] = {}

            rot  = _safe_float(data, ["opticalRotation", "rotation", "alpha", "angle"])
            temp = _safe_float(data, ["temperature", "Temperature", "temp"])
            conc = _safe_float(data, ["concentration", "Concentration", "conc"])
            wl   = _safe_float(data, ["wavelength", "Wavelength", "lambda"])
            conc_unit = _first_value(data, ["unit", "concUnit", "concentrationUnit"]) or "%"

            if rot  is not None: metrics["opticalRotation"] = {"value": rot,  "unit": "°"}
            if temp is not None: metrics["temperature"]     = {"value": temp, "unit": "Celsius"}
            if conc is not None: metrics["concentration"]   = {"value": conc, "unit": conc_unit}
            if wl   is not None: metrics["wavelength"]      = {"value": wl,   "unit": "nm"}

            if not metrics:
                return {"status": "error", "message": "API-Z: no recognisable fields in response"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except aiohttp.ClientConnectorError:
            return {"status": "error", "message": f"API-Z: cannot connect to {ip}:{port}"}
        except asyncio.TimeoutError:
            return {"status": "error", "message": f"API-Z: request timed out ({TIMEOUT_SEC}s)"}
        except Exception as e:
            logger.exception("API-Z: unexpected error")
            return {"status": "error", "message": f"API-Z plugin error: {e}"}


# ── helpers ─────────────────────────────────────────────────────────────────

def _safe_float(d: dict, keys: list):
    for k in keys:
        if k in d:
            try:
                return round(float(d[k]), 4)
            except (ValueError, TypeError):
                return None
    return None


def _first_value(d: dict, keys: list):
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None
