# plugins/hach_dr6000_shared/integration.py
"""
HACH DR6000 Spectrophotometer — Shared Folder Plugin
Connectivity: USB Type B (exports CSV or JSON to USB drive → mounted shared folder)

The DR6000 does not have a LAN port. It exports measurement data to a USB drive.
This plugin polls a shared/mounted folder path for the latest export file and
parses it into the InstruLog metrics format.

Config keys (set on the instrument record):
  shared_folder  — absolute path to the mounted USB/shared folder (e.g. /mnt/dr6000)
  file_pattern   — glob pattern for export files (default: "*.csv")
  file_format    — "csv" or "json" (default: "csv")

Default port: unused (set to 0 or leave blank)

Note: The DR6000 supports multi-wavelength scans. If the export contains
multiple wavelength columns, each is reported as a separate metric.
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


class HachDR6000SharedIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "hach_dr6000_shared_folder"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        shared_folder = config.get("shared_folder", "")
        if not shared_folder:
            raise ValueError("DR6000: 'shared_folder' must be set in instrument config")
        if not os.path.isdir(shared_folder):
            raise FileNotFoundError(f"DR6000: shared folder not found: {shared_folder}")
        logger.info("DR6000: shared folder OK: %s", shared_folder)
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        shared_folder = self._config.get("shared_folder", "")
        file_pattern  = self._config.get("file_pattern", "*.csv")
        file_format   = self._config.get("file_format", "csv").lower()

        if file_format not in SUPPORTED_FORMATS:
            return {"status": "error", "message": f"DR6000: unsupported file_format '{file_format}'"}

        try:
            pattern = os.path.join(shared_folder, file_pattern)
            files   = await asyncio.to_thread(glob.glob, pattern)
            if not files:
                return {"status": "error", "message": f"DR6000: no files matching '{pattern}'"}

            latest = max(files, key=os.path.getmtime)
            logger.debug("DR6000: reading file: %s", latest)

            if file_format == "csv":
                return await self._parse_csv(latest)
            else:
                return await self._parse_json(latest)

        except Exception as e:
            logger.exception("DR6000: unexpected error")
            return {"status": "error", "message": f"DR6000 plugin error: {e}"}

    async def _parse_csv(self, filepath: str) -> Dict[str, Any]:
        """
        DR6000 CSV export columns (Hach default):
          Date, Time, Sample ID, Program, Wavelength (nm), Absorbance, Concentration, Unit
        For multi-wavelength scans, multiple rows share the same Sample ID.
        """
        try:
            def _read():
                with open(filepath, newline="", encoding="utf-8-sig") as f:
                    return list(csv.DictReader(f))

            rows = await asyncio.to_thread(_read)
            if not rows:
                return {"status": "error", "message": "DR6000: CSV file is empty"}

            rows = [{k.strip(): v.strip() for k, v in r.items()} for r in rows]

            # Group by sample/timestamp — use the latest sample (last unique block)
            last_sample_id = _first_value(rows[-1], ["Sample ID", "SampleID", "ID"]) or "__last__"
            sample_rows = [
                r for r in rows
                if (_first_value(r, ["Sample ID", "SampleID", "ID"]) or "__last__") == last_sample_id
            ] or [rows[-1]]

            metrics: Dict[str, Any] = {}

            for row in sample_rows:
                wl = _safe_float(row, ["Wavelength (nm)", "Wavelength", "wavelength"])
                ab = _safe_float(row, ["Absorbance", "Abs", "absorbance"])
                cn = _safe_float(row, ["Concentration", "Conc", "concentration"])
                unit_str = _first_value(row, ["Unit", "unit", "Conc. Unit"]) or "mg/L"

                # Key per wavelength if multi-wavelength, else generic
                suffix = f"_{int(wl)}nm" if wl is not None else ""
                if ab is not None:
                    metrics[f"absorbance{suffix}"] = {"value": ab, "unit": "Abs"}
                if cn is not None:
                    metrics[f"concentration{suffix}"] = {"value": cn, "unit": unit_str}

            # Report wavelength as a single metric only for single-wavelength exports
            if len(sample_rows) == 1:
                wl = _safe_float(sample_rows[0], ["Wavelength (nm)", "Wavelength", "wavelength"])
                if wl is not None:
                    metrics["wavelength"] = {"value": wl, "unit": "nm"}

            if not metrics:
                return {"status": "error", "message": "DR6000: no recognisable metric columns in CSV"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except Exception as e:
            return {"status": "error", "message": f"DR6000 CSV parse error: {e}"}

    async def _parse_json(self, filepath: str) -> Dict[str, Any]:
        """
        DR6000 JSON export structure (same schema as DR3900, may include wavelengthScan array):
        {
          "readings": [
            { "absorbance": 0.45, "concentration": 8.2, "unit": "mg/L", "wavelength": 510 }
          ]
        }
        """
        try:
            def _read():
                with open(filepath, encoding="utf-8") as f:
                    return json.load(f)

            data = await asyncio.to_thread(_read)

            if isinstance(data, list):
                records = data
            elif isinstance(data, dict) and "readings" in data:
                records = data["readings"]
            else:
                records = [data]

            if not records:
                return {"status": "error", "message": "DR6000: JSON export is empty"}

            # Latest measurement
            record = records[-1]
            scan   = record.get("wavelengthScan", [])

            metrics: Dict[str, Any] = {}

            if scan:
                # Multi-wavelength scan: [ { "nm": 420, "abs": 0.21 }, ... ]
                for point in scan:
                    wl = _safe_float(point, ["nm", "wavelength", "Wavelength"])
                    ab = _safe_float(point, ["abs", "absorbance", "Absorbance"])
                    if wl is not None and ab is not None:
                        metrics[f"absorbance_{int(wl)}nm"] = {"value": ab, "unit": "Abs"}
            else:
                ab   = _safe_float(record, ["absorbance", "Absorbance", "abs"])
                cn   = _safe_float(record, ["concentration", "Concentration", "conc"])
                wl   = _safe_float(record, ["wavelength", "Wavelength"])
                unit_str = _first_value(record, ["unit", "Unit", "concUnit"]) or "mg/L"

                if ab is not None:
                    metrics["absorbance"]    = {"value": ab, "unit": "Abs"}
                if cn is not None:
                    metrics["concentration"] = {"value": cn, "unit": unit_str}
                if wl is not None:
                    metrics["wavelength"]    = {"value": wl, "unit": "nm"}

            if not metrics:
                return {"status": "error", "message": "DR6000: no recognisable fields in JSON"}

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": metrics,
            }

        except Exception as e:
            return {"status": "error", "message": f"DR6000 JSON parse error: {e}"}


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
