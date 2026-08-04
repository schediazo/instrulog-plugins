# plugins/hach_dr3900_shared/integration.py
"""
HACH DR3900 Spectrophotometer — Shared Folder Plugin
Connectivity: USB Type B (exports CSV or JSON to USB drive → mounted shared folder)

The DR3900 does not have a LAN port. It exports measurement data to a USB drive.
This plugin polls a shared/mounted folder path for the latest export file and
parses it into the InstruLog metrics format.

Config keys (set on the instrument record):
  shared_folder  — absolute path to the mounted USB/shared folder (e.g. /mnt/dr3900)
  file_pattern   — glob pattern for export files (default: "*.csv")
  file_format    — "csv" or "json" (default: "csv")

Default port: unused (set to 0 or leave blank)
"""

import asyncio
import csv
import glob
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

SUPPORTED_FORMATS = ("csv", "json")


class HachDR3900SharedIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "hach_dr3900_shared_folder"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        shared_folder = config.get("shared_folder", "")
        if not shared_folder:
            raise ValueError("DR3900: 'shared_folder' must be set in instrument config")
        if not os.path.isdir(shared_folder):
            raise FileNotFoundError(f"DR3900: shared folder not found: {shared_folder}")
        logger.info("DR3900: shared folder OK: %s", shared_folder)
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        shared_folder = self._config.get("shared_folder", "")
        file_pattern  = self._config.get("file_pattern", "*.csv")
        file_format   = self._config.get("file_format", "csv").lower()

        if file_format not in SUPPORTED_FORMATS:
            return {"status": "error", "message": f"DR3900: unsupported file_format '{file_format}' (use csv or json)"}

        try:
            pattern = os.path.join(shared_folder, file_pattern)
            files   = await asyncio.to_thread(glob.glob, pattern)
            if not files:
                return {"status": "error", "message": f"DR3900: no files matching '{pattern}'"}

            latest = max(files, key=os.path.getmtime)
            logger.debug("DR3900: reading file: %s", latest)

            if file_format == "csv":
                return await self._parse_csv(latest)
            else:
                return await self._parse_json(latest)

        except Exception as e:
            logger.exception("DR3900: unexpected error")
            return {"status": "error", "message": f"DR3900 plugin error: {e}"}

    async def _parse_csv(self, filepath: str) -> Dict[str, Any]:
        """
        Expected DR3900 CSV export columns (Hach default export):
          Date, Time, Sample ID, Program, Wavelength (nm), Absorbance, Concentration, Unit
        """
        try:
            def _read():
                with open(filepath, newline="", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                return rows

            rows = await asyncio.to_thread(_read)
            if not rows:
                return {"status": "error", "message": "DR3900: CSV file is empty"}

            # Use the last row as the most recent measurement
            row = rows[-1]

            # Normalise keys (strip whitespace, lower for lookup)
            row = {k.strip(): v.strip() for k, v in row.items()}

            absorbance    = _safe_float(row, ["Absorbance", "Abs", "absorbance"])
            concentration = _safe_float(row, ["Concentration", "Conc", "concentration"])
            wavelength    = _safe_float(row, ["Wavelength (nm)", "Wavelength", "wavelength"])
            unit_str      = _first_value(row, ["Unit", "unit", "Conc. Unit"]) or "mg/L"

            metrics: Dict[str, Any] = {}
            if absorbance is not None:
                metrics["absorbance"]    = {"value": absorbance, "unit": "Abs"}
            if concentration is not None:
                metrics["concentration"] = {"value": concentration, "unit": unit_str}
            if wavelength is not None:
                metrics["wavelength"]    = {"value": wavelength, "unit": "nm"}

            if not metrics:
                return {"status": "error", "message": "DR3900: no recognisable metric columns in CSV"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except Exception as e:
            return {"status": "error", "message": f"DR3900 CSV parse error: {e}"}

    async def _parse_json(self, filepath: str) -> Dict[str, Any]:
        """
        Expected DR3900 JSON export structure:
        { "readings": [ { "absorbance": 0.312, "concentration": 5.4, "unit": "mg/L",
                           "wavelength": 420, "programName": "..." } ] }
        or a single-object export:
        { "absorbance": 0.312, "concentration": 5.4, "unit": "mg/L", "wavelength": 420 }
        """
        try:
            def _read():
                with open(filepath, encoding="utf-8") as f:
                    return json.load(f)

            data = await asyncio.to_thread(_read)

            # Support both array-of-readings and flat single object
            if isinstance(data, list):
                record = data[-1] if data else {}
            elif isinstance(data, dict) and "readings" in data:
                readings = data["readings"]
                record   = readings[-1] if readings else {}
            else:
                record = data

            absorbance    = _safe_float(record, ["absorbance", "Absorbance", "abs"])
            concentration = _safe_float(record, ["concentration", "Concentration", "conc"])
            wavelength    = _safe_float(record, ["wavelength", "Wavelength"])
            unit_str      = _first_value(record, ["unit", "Unit", "concUnit"]) or "mg/L"

            metrics: Dict[str, Any] = {}
            if absorbance is not None:
                metrics["absorbance"]    = {"value": absorbance, "unit": "Abs"}
            if concentration is not None:
                metrics["concentration"] = {"value": concentration, "unit": unit_str}
            if wavelength is not None:
                metrics["wavelength"]    = {"value": wavelength, "unit": "nm"}

            if not metrics:
                return {"status": "error", "message": "DR3900: no recognisable fields in JSON"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except Exception as e:
            return {"status": "error", "message": f"DR3900 JSON parse error: {e}"}


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
