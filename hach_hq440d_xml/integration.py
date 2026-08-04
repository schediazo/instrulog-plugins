# plugins/hach_hq440d_xml/integration.py
"""
HACH HQ440d Multimeter (pH / Conductivity / DO) — Shared Folder Plugin (XML)
Connectivity: USB Type A (exports XML to USB drive → mounted shared folder)

Config keys:
  shared_folder  — absolute path to mounted folder (e.g. /mnt/hq440d)
  file_pattern   — glob pattern (default: "*.xml")

Expected XML structure (Hach HQd series XML export):
<HachData>
  <Measurement>
    <DateTime>2024-01-15T10:30:00</DateTime>
    <Probe type="pH">
      <Result unit="pH">7.21</Result>
      <Temperature unit="°C">23.5</Temperature>
    </Probe>
    <Probe type="Conductivity">
      <Result unit="µS/cm">425.0</Result>
    </Probe>
    <Probe type="DO">
      <Result unit="mg/L">8.1</Result>
    </Probe>
  </Measurement>
</HachData>

Note: Only stdlib xml.etree.ElementTree is used — no external dependencies.
"""

import asyncio
import glob
import logging
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HachHQ440dXmlIntegration:

    def __init__(self):
        self._config: Dict[str, Any] = {}

    @property
    def instrument_type(self) -> str:
        return "hach_hq440d_shared_xml"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        self._config = config
        folder = config.get("shared_folder", "")
        if not folder:
            raise ValueError("HQ440d XML: 'shared_folder' must be set in instrument config")
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"HQ440d XML: shared folder not found: {folder}")
        logger.info("HQ440d XML: shared folder OK: %s", folder)
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        folder       = self._config.get("shared_folder", "")
        file_pattern = self._config.get("file_pattern", "*.xml")

        try:
            pattern = os.path.join(folder, file_pattern)
            files   = await asyncio.to_thread(glob.glob, pattern)
            if not files:
                return {"status": "error", "message": f"HQ440d XML: no files matching '{pattern}'"}

            latest = max(files, key=os.path.getmtime)
            logger.debug("HQ440d XML: reading %s", latest)

            def _parse():
                return ET.parse(latest).getroot()

            root = await asyncio.to_thread(_parse)
            return self._extract_metrics(root)

        except ET.ParseError as e:
            return {"status": "error", "message": f"HQ440d XML: malformed XML: {e}"}
        except Exception as e:
            logger.exception("HQ440d XML: unexpected error")
            return {"status": "error", "message": f"HQ440d XML plugin error: {e}"}

    def _extract_metrics(self, root: ET.Element) -> Dict[str, Any]:
        """
        Walk the XML tree and extract the last Measurement block.
        Handles both <HachData><Measurement>...</Measurement></HachData>
        and flat <Measurement>...</Measurement> root elements.
        """
        # Find all Measurement elements (case-insensitive tag search)
        measurements = (
            root.findall(".//Measurement")
            or root.findall(".//measurement")
            or ([root] if root.tag.lower() == "measurement" else [])
        )

        if not measurements:
            return {"status": "error", "message": "HQ440d XML: no <Measurement> elements found"}

        measurement = measurements[-1]  # latest
        metrics: Dict[str, Any] = {}
        temp_reported = False

        probes = measurement.findall("Probe") or measurement.findall("probe")

        if probes:
            # Structured multi-probe export
            for probe in probes:
                probe_type = (probe.get("type") or probe.get("Type") or "").lower()

                result_el  = probe.find("Result") or probe.find("result")
                result_val = _elem_float(result_el)
                result_unit = (result_el.get("unit") or result_el.get("Unit") or "") if result_el is not None else ""

                temp_el   = probe.find("Temperature") or probe.find("temperature")
                temp_val  = _elem_float(temp_el)
                temp_unit = (temp_el.get("unit") or "Celsius").replace("°C", "Celsius") if temp_el is not None else "Celsius"

                if result_val is not None:
                    if "ph" in probe_type:
                        metrics["pH"] = {"value": result_val, "unit": result_unit or "pH"}
                    elif "conductivity" in probe_type or "cond" in probe_type:
                        metrics["conductivity"] = {"value": result_val, "unit": result_unit or "µS/cm"}
                    elif "oxygen" in probe_type or probe_type == "do":
                        metrics["dissolvedOxygen"] = {"value": result_val, "unit": result_unit or "mg/L"}
                    else:
                        key = probe_type.replace(" ", "_") or "reading"
                        metrics[key] = {"value": result_val, "unit": result_unit}

                if not temp_reported and temp_val is not None:
                    metrics["temperature"] = {"value": temp_val, "unit": temp_unit}
                    temp_reported = True

        else:
            # Flat export — direct child elements
            ph   = _child_float(measurement, ["pH", "ph", "Ph"])
            temp = _child_float(measurement, ["Temperature", "temperature", "Temp"])
            cond = _child_float(measurement, ["Conductivity", "conductivity", "Cond"])
            do_  = _child_float(measurement, ["DissolvedOxygen", "dissolvedOxygen", "DO", "do"])

            if ph   is not None: metrics["pH"]              = {"value": ph,   "unit": "pH"}
            if temp is not None: metrics["temperature"]     = {"value": temp, "unit": "Celsius"}
            if cond is not None: metrics["conductivity"]    = {"value": cond, "unit": "µS/cm"}
            if do_  is not None: metrics["dissolvedOxygen"] = {"value": do_,  "unit": "mg/L"}

        if not metrics:
            return {"status": "error", "message": "HQ440d XML: no recognisable metric elements found"}

        return {
            "status": "success",
            "instrument_type": self.instrument_type,
            "metrics": metrics,
        }


# ── helpers ─────────────────────────────────────────────────────────────────

def _elem_float(el: Optional[ET.Element]) -> Optional[float]:
    if el is None or not el.text:
        return None
    try:
        return round(float(el.text.strip()), 4)
    except (ValueError, TypeError):
        return None


def _child_float(parent: ET.Element, tags: list) -> Optional[float]:
    for tag in tags:
        child = parent.find(tag)
        if child is not None:
            return _elem_float(child)
    return None
