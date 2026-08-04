# plugins/hach_hq440d_csv/integration.py
"""
HACH HQ440d Multimeter (pH / Conductivity / DO) — Shared Folder Plugin (CSV)
Connectivity: USB Type A (exports CSV to USB drive → mounted shared folder)

Config keys:
  shared_folder  — absolute path to mounted folder (e.g. /mnt/hq440d)
  file_pattern   — glob pattern (default: "*.csv")

Expected CSV columns (Hach HQ440d default export):
  Date, Time, Sensor S/N, Reading Type, Result, Unit, Temperature, Temp Unit

Multiple probe readings may appear as separate rows for the same timestamp.
"""

import asyncio
import csv
import glob
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class HachHQ440dCsvIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "hach_hq440d_shared_csv"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        folder = config.get("shared_folder", "")
        if not folder:
            raise ValueError("HQ440d CSV: 'shared_folder' must be set in instrument config")
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"HQ440d CSV: shared folder not found: {folder}")
        logger.info("HQ440d CSV: shared folder OK: %s", folder)
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        folder       = self._config.get("shared_folder", "")
        file_pattern = self._config.get("file_pattern", "*.csv")

        try:
            pattern = os.path.join(folder, file_pattern)
            files   = await asyncio.to_thread(glob.glob, pattern)
            if not files:
                return {"status": "error", "message": f"HQ440d CSV: no files matching '{pattern}'"}

            latest = max(files, key=os.path.getmtime)
            logger.debug("HQ440d CSV: reading %s", latest)

            def _read():
                with open(latest, newline="", encoding="utf-8-sig") as f:
                    return list(csv.DictReader(f))

            rows = await asyncio.to_thread(_read)
            if not rows:
                return {"status": "error", "message": "HQ440d CSV: file is empty"}

            rows = [{k.strip(): v.strip() for k, v in r.items()} for r in rows]

            # HQ440d exports one row per sensor per measurement session.
            # Group by the last timestamp to collect all probes from the latest reading.
            date_key = _key_match(rows[0], ["Date", "date"])
            time_key = _key_match(rows[0], ["Time", "time"])
            last_ts  = (rows[-1].get(date_key, ""), rows[-1].get(time_key, ""))

            sample_rows = [
                r for r in rows
                if (r.get(date_key, ""), r.get(time_key, "")) == last_ts
            ] or [rows[-1]]

            metrics: Dict[str, Any] = {}
            temp_reported = False

            for row in sample_rows:
                reading_type = _first_value(row, ["Reading Type", "ReadingType", "Parameter", "Measurement"]) or ""
                result       = _safe_float(row, ["Result", "result", "Value", "value"])
                unit         = _first_value(row, ["Unit", "unit", "Units"]) or ""
                temp_val     = _safe_float(row, ["Temperature", "temperature", "Temp"])
                temp_unit    = _first_value(row, ["Temp Unit", "TempUnit", "Temp_Unit"]) or "Celsius"

                rt_lower = reading_type.lower()

                if result is not None:
                    if "ph" in rt_lower:
                        metrics["pH"] = {"value": result, "unit": "pH"}
                    elif "conductivity" in rt_lower or "cond" in rt_lower:
                        metrics["conductivity"] = {"value": result, "unit": unit or "µS/cm"}
                    elif "oxygen" in rt_lower or " do" in rt_lower or rt_lower == "do":
                        metrics["dissolvedOxygen"] = {"value": result, "unit": unit or "mg/L"}
                    else:
                        # Unknown probe — store with sanitised key
                        key = rt_lower.replace(" ", "_") or "reading"
                        metrics[key] = {"value": result, "unit": unit}

                # Temperature is reported per-probe row; add once
                if not temp_reported and temp_val is not None:
                    metrics["temperature"] = {"value": temp_val, "unit": temp_unit}
                    temp_reported = True

            if not metrics:
                return {"status": "error", "message": "HQ440d CSV: no recognisable metric columns"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except Exception as e:
            logger.exception("HQ440d CSV: unexpected error")
            return {"status": "error", "message": f"HQ440d CSV plugin error: {e}"}


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


def _key_match(d: dict, candidates: list) -> str:
    for k in candidates:
        if k in d:
            return k
    return candidates[0]
