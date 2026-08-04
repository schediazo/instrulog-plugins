# plugins/hach_hq440d_json/integration.py
"""
HACH HQ440d Multimeter (pH / Conductivity / DO) — Shared Folder Plugin (JSON)
Connectivity: USB Type A (exports JSON to USB drive → mounted shared folder)

Config keys:
  shared_folder  — absolute path to mounted folder (e.g. /mnt/hq440d)
  file_pattern   — glob pattern (default: "*.json")

Metrics reported: pH, temperature, conductivity, dissolvedOxygen
(only metrics present in the export are included)
"""

import asyncio
import glob
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class HachHQ440dJsonIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "hach_hq440d_shared_json"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        folder = config.get("shared_folder", "")
        if not folder:
            raise ValueError("HQ440d: 'shared_folder' must be set in instrument config")
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"HQ440d: shared folder not found: {folder}")
        logger.info("HQ440d JSON: shared folder OK: %s", folder)
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        folder       = self._config.get("shared_folder", "")
        file_pattern = self._config.get("file_pattern", "*.json")

        try:
            pattern = os.path.join(folder, file_pattern)
            files   = await asyncio.to_thread(glob.glob, pattern)
            if not files:
                return {"status": "error", "message": f"HQ440d: no files matching '{pattern}'"}

            latest = max(files, key=os.path.getmtime)
            logger.debug("HQ440d JSON: reading %s", latest)

            def _read():
                with open(latest, encoding="utf-8") as f:
                    return json.load(f)

            data = await asyncio.to_thread(_read)

            # Support { "readings": [...] } or [ {...} ] or { ...single... }
            if isinstance(data, list):
                record = data[-1] if data else {}
            elif isinstance(data, dict) and "readings" in data:
                readings = data["readings"]
                record   = readings[-1] if readings else {}
            else:
                record = data

            metrics: Dict[str, Any] = {}

            ph   = _safe_float(record, ["pH", "ph", "pH_value"])
            temp = _safe_float(record, ["temperature", "Temperature", "temp", "Temp"])
            cond = _safe_float(record, ["conductivity", "Conductivity", "cond"])
            do_  = _safe_float(record, ["dissolvedOxygen", "DO", "do", "dissolved_oxygen"])

            # Units — HQ440d may include them in the export
            temp_unit = _first_value(record, ["temperatureUnit", "tempUnit"]) or "Celsius"
            cond_unit = _first_value(record, ["conductivityUnit", "condUnit"]) or "µS/cm"
            do_unit   = _first_value(record, ["doUnit", "DOUnit"])             or "mg/L"

            if ph   is not None: metrics["pH"]              = {"value": ph,   "unit": "pH"}
            if temp is not None: metrics["temperature"]     = {"value": temp, "unit": temp_unit}
            if cond is not None: metrics["conductivity"]    = {"value": cond, "unit": cond_unit}
            if do_  is not None: metrics["dissolvedOxygen"] = {"value": do_,  "unit": do_unit}

            if not metrics:
                return {"status": "error", "message": "HQ440d: no recognisable fields in JSON export"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except Exception as e:
            logger.exception("HQ440d JSON: unexpected error")
            return {"status": "error", "message": f"HQ440d JSON plugin error: {e}"}


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
