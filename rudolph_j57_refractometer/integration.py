# plugins/rudolph_j57_refractometer/integration.py
"""
Rudolph Research J57 Refractometer — LAN Plugin (REST / HTTP)
Connectivity: USB, RS232, Printer, LAN

The J57 exposes an HTTP endpoint on its LAN port for data retrieval.
This plugin polls the REST endpoint at the configured IP/port.

Config keys:
  ip    — instrument IP address
  port  — HTTP port (default: 80)

Metrics reported:
  refractiveIndex   (nD — refractive index, dimensionless)
  temperature       (Celsius)
  brix              (%, optional — if Brix scale is active)
  concentration     (%, optional — if concentration method is configured)

Rudolph J57 HTTP response (typical JSON):
{
  "status": "OK",
  "refractiveIndex": 1.36420,
  "temperature": 20.00,
  "brix": 32.5,
  "concentration": 28.4,
  "concentrationUnit": "%w/w"
}
"""

import asyncio
import aiohttp
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

TIMEOUT_SEC = 10


class RudolphJ57RefractometerIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "rudolph_j57_refractometer_rest"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        ip   = self._config.get("ip", "192.168.1.101")
        port = self._config.get("port", 80)
        url  = f"http://{ip}:{port}/api/measurement/latest"

        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {
                            "status": "error",
                            "message": f"J57 HTTP {resp.status} from {url}",
                        }
                    data = await resp.json(content_type=None)

            instr_status = str(data.get("status", "OK")).upper()
            if instr_status not in ("OK", "READY", "SUCCESS", ""):
                return {
                    "status": "error",
                    "message": f"J57 instrument status: {instr_status}",
                }

            metrics: Dict[str, Any] = {}

            ri   = _safe_float(data, ["refractiveIndex", "nD", "ri", "refractive_index"])
            temp = _safe_float(data, ["temperature", "Temperature", "temp"])
            brix = _safe_float(data, ["brix", "Brix", "BRIX"])
            conc = _safe_float(data, ["concentration", "Concentration", "conc"])
            conc_unit = _first_value(data, ["concentrationUnit", "concUnit", "unit"]) or "%"

            if ri   is not None: metrics["refractiveIndex"] = {"value": ri,   "unit": "nD"}
            if temp is not None: metrics["temperature"]     = {"value": temp, "unit": "Celsius"}
            if brix is not None: metrics["brix"]            = {"value": brix, "unit": "%Brix"}
            if conc is not None: metrics["concentration"]   = {"value": conc, "unit": conc_unit}

            if not metrics:
                return {"status": "error", "message": "J57: no recognisable fields in response"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except aiohttp.ClientConnectorError:
            return {"status": "error", "message": f"J57: cannot connect to {ip}:{port}"}
        except asyncio.TimeoutError:
            return {"status": "error", "message": f"J57: request timed out ({TIMEOUT_SEC}s)"}
        except Exception as e:
            logger.exception("J57: unexpected error")
            return {"status": "error", "message": f"J57 plugin error: {e}"}


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
