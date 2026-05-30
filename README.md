# InstruLog Plugin Development Guide

A complete reference for writing, testing, and publishing your own instrument integration plugins for the InstruLog LIMS backend.

---

## Table of Contents

1. [How the Plugin System Works](#1-how-the-plugin-system-works)
2. [Plugin File Structure](#2-plugin-file-structure)
3. [The Integration Class Contract](#3-the-integration-class-contract)
4. [Method Reference](#4-method-reference)
5. [The Parsed Data Format](#5-the-parsed-data-format)
6. [Writing a Modbus TCP Plugin](#6-writing-a-modbus-tcp-plugin)
7. [Writing a REST API Plugin](#7-writing-a-rest-api-plugin)
8. [Testing Your Plugin Locally](#8-testing-your-plugin-locally)
9. [Publishing to GitHub](#9-publishing-to-github)
10. [Registering and Installing via InstruLog](#10-registering-and-installing-via-instrulog)
11. [Error Handling Best Practices](#11-error-handling-best-practices)
12. [Full Reference Example](#12-full-reference-example)
13. [FAQ](#13-faq)

---

## 1. How the Plugin System Works

When InstruLog starts, the `plugin_manager` scans the `plugins/` directory for subdirectories that contain an `integration.py` file. It dynamically loads each one and looks for a class that implements three things:

- An `instrument_type` property
- A `connect()` async method
- A `parse_to_json()` async method

Once loaded, the `stream_engine` uses your plugin to:

1. **Connect** to the physical instrument over the network
2. **Poll** it at the configured interval (e.g. every 2000ms)
3. **Parse** the raw bytes or HTTP response into a standard JSON structure
4. **Store** the result in the database as an `InstrumentReading`
5. **Push** it live to the frontend via SSE (Server-Sent Events)

```
Instrument (hardware)
      │
      │  TCP / HTTP
      ▼
plugin.connect()  ──►  reader, writer
      │
      │  raw bytes / HTTP response
      ▼
plugin.parse_to_json()  ──►  { status, metrics: { pH: {...}, ... } }
      │
      ▼
InstrumentReading saved to DB  ──►  SSE pushed to frontend
```

---

## 2. Plugin File Structure

Every plugin is a folder inside `plugins/` with exactly this layout:

```
plugins/
└── your_plugin_name/         ← folder name = plugin "name" used during registration
    ├── __init__.py           ← empty file, required for Python import
    └── integration.py        ← your plugin code lives here
```

The folder name must:
- Be lowercase with underscores only (e.g. `rudolph_modbus`, `omega_ph_rest`)
- Match the `name` field you use when registering the plugin in InstruLog
- Contain no spaces or special characters

---

## 3. The Integration Class Contract

Your `integration.py` must define **one class** (any name) that has these three members:

```python
class YourPluginIntegration:

    @property
    def instrument_type(self) -> str:
        """
        A unique string identifier for this instrument type.
        Stored with every reading. Use snake_case.
        Example: "rudolph_modbus_tcp", "omega_ph_rest"
        """
        ...

    async def connect(self, config: dict) -> tuple:
        """
        Establish a connection to the instrument.
        Returns (reader, writer) for TCP plugins.
        For REST plugins, can return (None, None) since
        HTTP connections are stateless per-request.
        """
        ...

    async def parse_to_json(self, raw_bytes: bytes) -> dict:
        """
        Parse raw data from the instrument into the standard
        InstruLog metrics format.
        Must always return a dict — never raise unhandled exceptions.
        """
        ...
```

The plugin manager discovers your class automatically by checking that it has all three members — the class name itself does not matter.

---

## 4. Method Reference

### `instrument_type` (property)

```python
@property
def instrument_type(self) -> str:
    return "my_instrument_tcp"
```

- Must be a non-empty string
- Should uniquely identify the instrument model and protocol
- Stored in every `parsed_data` JSON blob for traceability
- Convention: `{brand}_{model}_{protocol}` e.g. `rudolph_ph100_modbus_tcp`

---

### `connect(config)` (async method)

```python
async def connect(self, config: dict) -> tuple:
```

**`config` keys provided by the stream engine:**

| Key | Type | Description |
|-----|------|-------------|
| `ip` | `str` | Host/IP from the instrument record |
| `port` | `int` | Port from the instrument record |

**Returns:** `(reader, writer)` tuple

- For **Modbus TCP / raw TCP**: use `asyncio.open_connection()`
- For **REST API**: return `(None, None)` — do your HTTP call inside `parse_to_json()` instead
- If connection fails, let the exception propagate — the stream engine handles retries and error counting

**Modbus TCP example:**
```python
async def connect(self, config: dict) -> tuple:
    ip = config.get("ip", "127.0.0.1")
    port = config.get("port", 502)
    reader, writer = await asyncio.open_connection(ip, port)
    return reader, writer
```

**REST API example:**
```python
async def connect(self, config: dict) -> tuple:
    # Store config for use in parse_to_json
    self._config = config
    return None, None
```

---

### `parse_to_json(raw_bytes)` (async method)

```python
async def parse_to_json(self, raw_bytes: bytes) -> dict:
```

**Parameter:**
- `raw_bytes` — the raw data read from the TCP connection (`reader.read(1024)`)
- For REST plugins this will be empty/unused — make your HTTP request here instead

**Must return** a dict in the standard InstruLog format (see section 5).

**Must never raise** — always catch exceptions internally and return an error dict.

---

## 5. The Parsed Data Format

Every plugin must return data in this exact structure. The frontend and database both depend on it.

### Success response

```json
{
    "status": "success",
    "instrument_type": "your_instrument_type",
    "transaction_id": 1,
    "metrics": {
        "metricName": {
            "value": 7.21,
            "unit": "pH"
        },
        "anotherMetric": {
            "value": 23.8,
            "unit": "Celsius"
        }
    }
}
```

### Error response

```json
{
    "status": "error",
    "message": "Human-readable description of what went wrong"
}
```

### Field definitions

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `status` | ✅ | `"success"` or `"error"` | Determines how the reading is stored and displayed |
| `instrument_type` | ✅ on success | `str` | Must match your `instrument_type` property |
| `transaction_id` | optional | `int` | Modbus transaction ID or request sequence number |
| `metrics` | ✅ on success | `dict` | One key per measurement |
| `metrics.X.value` | ✅ | `float` or `int` | The numeric reading |
| `metrics.X.unit` | ✅ | `str` | Unit string shown in the frontend table column header |
| `message` | ✅ on error | `str` | Error description stored in `error_message` column |

### Metric naming rules

- Use camelCase for metric keys: `pH`, `temperature`, `dissolvedOxygen`, `conductivity`
- The frontend uses the metric key + unit to build column headers automatically: `pH (pH)`, `temperature (Celsius)`
- You can return as many metrics as your instrument supports — the frontend adapts dynamically

---

## 6. Writing a Modbus TCP Plugin

Modbus TCP is the most common protocol for lab instruments. Here is a complete walkthrough.

### Understanding Modbus TCP packet structure

A standard Modbus TCP response frame:

```
Byte 0-1:  Transaction ID   (2 bytes, big-endian uint16)
Byte 2-3:  Protocol ID      (2 bytes, always 0x0000)
Byte 4-5:  Length           (2 bytes, big-endian uint16)
Byte 6:    Unit ID          (1 byte)
Byte 7:    Function Code    (1 byte, 0x03 = Read Holding Registers)
Byte 8:    Byte Count       (1 byte)
Byte 9+:   Register data    (N bytes, big-endian uint16 per register)
```

### Complete Modbus TCP plugin

```python
# plugins/my_ph_meter/integration.py

import asyncio
import struct
from typing import Dict, Any


class MyPhMeterIntegration:

    @property
    def instrument_type(self) -> str:
        return "my_ph_meter_modbus_tcp"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip = config.get("ip", "127.0.0.1")
        port = config.get("port", 502)
        reader, writer = await asyncio.open_connection(ip, port)
        return reader, writer

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        # Always validate length first
        if len(raw_bytes) < 9:
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes"
            }

        try:
            # Unpack the MBAP header (first 7 bytes)
            transaction_id, protocol_id, length, unit_id = struct.unpack(
                ">HHHB", raw_bytes[:7]
            )

            function_code = raw_bytes[7]
            byte_count    = raw_bytes[8]

            # Only handle Function Code 3 (Read Holding Registers)
            if function_code != 0x03:
                return {
                    "status": "error",
                    "message": f"Unsupported function code: {function_code:#04x}"
                }

            # Each register is 2 bytes (uint16, big-endian)
            # Adjust the slice and scaling for your instrument's register map
            if len(raw_bytes) < 13:
                return {"status": "error", "message": "Not enough register data"}

            ph_raw, temp_raw = struct.unpack(">HH", raw_bytes[9:13])

            # Apply scaling factor from your instrument's datasheet
            # e.g. raw value 721 → 7.21 pH (divide by 100)
            ph_value   = ph_raw / 100.0
            temp_value = temp_raw / 100.0

            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "metrics": {
                    "pH": {
                        "value": ph_value,
                        "unit": "pH"
                    },
                    "temperature": {
                        "value": temp_value,
                        "unit": "Celsius"
                    }
                }
            }

        except struct.error as e:
            return {
                "status": "error",
                "message": f"Failed to unpack Modbus packet: {e}"
            }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Unexpected parse error: {e}"
            }
```

### Customising register parsing

Check your instrument's datasheet for the register map. Common patterns:

```python
# Single register (2 bytes) — 1 metric
value, = struct.unpack(">H", raw_bytes[9:11])

# Two registers (4 bytes) — 2 metrics
val1, val2 = struct.unpack(">HH", raw_bytes[9:13])

# Three registers (6 bytes) — 3 metrics
val1, val2, val3 = struct.unpack(">HHH", raw_bytes[9:15])

# 32-bit float register (4 bytes) — 1 float metric
val, = struct.unpack(">f", raw_bytes[9:13])

# Signed 16-bit (for negative values like temperature below 0)
val, = struct.unpack(">h", raw_bytes[9:11])  # lowercase h = signed
```

---

## 7. Writing a REST API Plugin

For instruments that expose an HTTP endpoint instead of raw TCP.

```python
# plugins/my_rest_instrument/integration.py

import aiohttp
from typing import Dict, Any


class MyRestInstrumentIntegration:

    def __init__(self):
        self._config = {}

    @property
    def instrument_type(self) -> str:
        return "my_rest_instrument_api"

    async def connect(self, config: Dict[str, Any]) -> tuple:
        # Store config — REST is stateless, no persistent connection needed
        self._config = config
        return None, None

    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        ip       = self._config.get("ip", "127.0.0.1")
        port     = self._config.get("port", 80)
        auth_key = self._config.get("auth_key", "")

        url = f"http://{ip}:{port}/api/readings/latest"

        headers = {}
        if auth_key:
            headers["Authorization"] = f"Bearer {auth_key}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return {
                            "status": "error",
                            "message": f"HTTP {resp.status} from instrument"
                        }
                    data = await resp.json()

            # Map your instrument's response fields to InstruLog metrics
            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "metrics": {
                    "pH": {
                        "value": float(data["ph_reading"]),
                        "unit": "pH"
                    },
                    "temperature": {
                        "value": float(data["temp_c"]),
                        "unit": "Celsius"
                    },
                    "dissolvedOxygen": {
                        "value": float(data["do_mgl"]),
                        "unit": "mg/L"
                    }
                }
            }

        except aiohttp.ClientConnectorError:
            return {"status": "error", "message": "Cannot connect to instrument HTTP endpoint"}
        except aiohttp.ClientTimeout:
            return {"status": "error", "message": "HTTP request timed out"}
        except KeyError as e:
            return {"status": "error", "message": f"Missing field in instrument response: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"REST plugin error: {e}"}
```

---

## 8. Testing Your Plugin Locally

Before publishing, test your plugin directly without the full backend.

### Quick standalone test script

Create `test_plugin.py` in the project root:

```python
# test_plugin.py
import asyncio
import sys
import os

# Add plugins dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "plugins"))

from your_plugin_name.integration import YourPluginIntegration


async def main():
    plugin = YourPluginIntegration()
    print(f"instrument_type: {plugin.instrument_type}")

    # Test connect
    try:
        reader, writer = await plugin.connect({"ip": "127.0.0.1", "port": 5020})
        print("✅ connect() succeeded")
    except Exception as e:
        print(f"❌ connect() failed: {e}")
        return

    # Test parse with a known raw packet
    # Replace this with real bytes from your instrument or a mock
    test_bytes = bytes.fromhex("000100000006010300040109096e")
    result = await plugin.parse_to_json(test_bytes)
    print(f"parse_to_json() → {result}")

    # Validate output format
    assert "status" in result, "Missing 'status' key"
    if result["status"] == "success":
        assert "metrics" in result, "Missing 'metrics' key on success"
        for key, val in result["metrics"].items():
            assert "value" in val, f"Metric '{key}' missing 'value'"
            assert "unit" in val,  f"Metric '{key}' missing 'unit'"
        print("✅ Output format is valid")
    else:
        assert "message" in result, "Error response missing 'message'"
        print(f"✅ Error response is valid: {result['message']}")

    if writer:
        writer.close()


asyncio.run(main())
```

Run it:
```bash
python test_plugin.py
```

### Test with a Modbus TCP simulator

If you don't have physical hardware, use `diagslave` or write a simple mock server:

```python
# mock_modbus_server.py — simulates an instrument on localhost:5020
import asyncio
import struct


async def handle_client(reader, writer):
    while True:
        data = await reader.read(1024)
        if not data:
            break

        # Parse request transaction ID
        transaction_id = struct.unpack(">H", data[0:2])[0]

        # Build a response: pH=7.21 (raw=721), temp=23.80 (raw=2380)
        ph_raw   = 721
        temp_raw = 2380
        payload  = struct.pack(">HH", ph_raw, temp_raw)

        response = struct.pack(">HHHBBB", transaction_id, 0, 7, 1, 3, 4) + payload
        writer.write(response)
        await writer.drain()

    writer.close()


async def main():
    server = await asyncio.start_server(handle_client, "127.0.0.1", 5020)
    print("Mock Modbus server running on 127.0.0.1:5020")
    async with server:
        await server.serve_forever()


asyncio.run(main())
```

---

## 9. Publishing to GitHub

The install system fetches `integration.py` directly from the **raw GitHub URL** of your plugin folder.

### Repository layout

You can host one plugin per repo, or multiple plugins in one repo:

```
# Option A — one plugin per repo
github.com/you/my-ph-plugin/
└── integration.py
└── __init__.py
└── README.md

# Option B — monorepo (recommended)
github.com/you/instrulog-plugins/
├── rudolph_modbus/
│   ├── integration.py
│   └── __init__.py
├── omega_rest/
│   ├── integration.py
│   └── __init__.py
└── README.md
```

### The `github_url` field

When registering your plugin in InstruLog, the `github_url` must point to the **raw directory base URL** — not the GitHub browser URL.

| URL Type | Example | Use? |
|----------|---------|------|
| GitHub browser | `github.com/you/repo/tree/main/my_plugin` | ❌ Returns HTML |
| Raw file | `raw.githubusercontent.com/you/repo/main/my_plugin/integration.py` | ❌ Backend appends `/integration.py` itself |
| **Raw directory** ✅ | `raw.githubusercontent.com/you/repo/main/my_plugin` | ✅ Correct |

The backend constructs the download URL as:
```python
integration_url = f"{github_url.rstrip('/')}/integration.py"
```

So your `github_url` should be:
```
https://raw.githubusercontent.com/your-username/your-repo/main/your_plugin_folder
```

**Example:**
```
https://raw.githubusercontent.com/schediazo/instrulog-plugins/main/rudolph_modbus
```

---

## 10. Registering and Installing via InstruLog

### Step 1 — Register the plugin

Go to **Integrations → Register Plugin** and fill in:

| Field | Example | Notes |
|-------|---------|-------|
| Plugin Slug (`name`) | `rudolph_modbus` | Must match your folder name exactly |
| Display Name | `Rudolph pH Meter` | Human-readable label shown in UI |
| Version | `1.0.0` | Semantic version |
| Connection Type | `Modbus TCP` | or `REST API` |
| Default Port | `5020` | Optional, pre-fills instrument form |
| Description | `Reads pH and temperature` | Optional |
| GitHub URL | `https://raw.githubusercontent.com/…/rudolph_modbus` | Raw directory URL |

### Step 2 — Install the plugin

Click **Install** on the plugin card. The backend will:

1. `GET {github_url}/integration.py` — download your file
2. Write it to `plugins/{name}/integration.py`
3. Dynamically import and load the class
4. Mark the plugin as `is_installed = True` in the database

### Step 3 — Add an instrument

Go to **Add → Instrument**, select your newly installed plugin, and configure the host/port/poll interval.

### Step 4 — Start streaming

Click **▶ Start Stream** on the instrument card. Live readings will appear immediately.

---

## 11. Error Handling Best Practices

The stream engine counts consecutive errors per instrument and sends an alert email after 5 failures. Follow these rules to avoid false alerts:

**Always return a dict, never raise:**
```python
# ❌ Bad — crashes the stream worker
async def parse_to_json(self, raw_bytes):
    val, = struct.unpack(">H", raw_bytes[9:11])  # IndexError if short packet
    return {"status": "success", ...}

# ✅ Good — stream continues after error
async def parse_to_json(self, raw_bytes):
    try:
        val, = struct.unpack(">H", raw_bytes[9:11])
        return {"status": "success", ...}
    except struct.error as e:
        return {"status": "error", "message": str(e)}
```

**Validate packet length early:**
```python
MINIMUM_PACKET_LENGTH = 13  # bytes required for your protocol

async def parse_to_json(self, raw_bytes):
    if len(raw_bytes) < MINIMUM_PACKET_LENGTH:
        return {
            "status": "error",
            "message": f"Short packet: got {len(raw_bytes)}, need {MINIMUM_PACKET_LENGTH}"
        }
    # ... safe to unpack now
```

**Use meaningful error messages** — they are stored in the database and displayed in the frontend logs table. Avoid generic messages like `"error"` or `"failed"`.

**Log for debugging** (optional but helpful):
```python
import logging
logger = logging.getLogger(__name__)

async def parse_to_json(self, raw_bytes):
    logger.debug("Raw bytes (%d): %s", len(raw_bytes), raw_bytes.hex())
    ...
```

---

## 12. Full Reference Example

A complete, well-commented plugin for a fictional multi-parameter water quality meter:

```python
# plugins/aquasense_pro/integration.py
"""
AquaSense Pro — Multi-parameter water quality meter
Protocol: Modbus TCP
Registers:
  0x00: pH          (uint16, scale /100, range 0–14)
  0x01: Temperature (uint16, scale /100, unit °C)
  0x02: Conductivity(uint16, scale /10,  unit µS/cm)
  0x03: Turbidity   (uint16, scale /100, unit NTU)
Default port: 5021
"""

import asyncio
import struct
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class AquaSenseProIntegration:

    # ── Required: instrument type identifier ─────────────────────────────────
    @property
    def instrument_type(self) -> str:
        return "aquasense_pro_modbus_tcp"

    # ── Required: establish TCP connection ───────────────────────────────────
    async def connect(self, config: Dict[str, Any]) -> tuple:
        ip   = config.get("ip",   "192.168.1.100")
        port = config.get("port", 5021)
        logger.info("AquaSense: connecting to %s:%s", ip, port)
        reader, writer = await asyncio.open_connection(ip, port)
        logger.info("AquaSense: connected")
        return reader, writer

    # ── Required: parse raw bytes into InstruLog metrics format ──────────────
    async def parse_to_json(self, raw_bytes: bytes) -> Dict[str, Any]:
        # 1. Validate minimum length (MBAP header=7 + FC=1 + ByteCount=1 + 4 registers×2=8 → 17 bytes)
        if len(raw_bytes) < 17:
            return {
                "status": "error",
                "message": f"Packet too short: {len(raw_bytes)} bytes (need 17)"
            }

        try:
            # 2. Unpack MBAP header
            transaction_id, protocol_id, length, unit_id = struct.unpack(
                ">HHHB", raw_bytes[:7]
            )
            function_code = raw_bytes[7]
            byte_count    = raw_bytes[8]

            # 3. Validate function code
            if function_code != 0x03:
                return {
                    "status": "error",
                    "message": f"Unexpected function code: {function_code:#04x} (expected 0x03)"
                }

            # 4. Unpack 4 registers starting at byte 9
            ph_raw, temp_raw, cond_raw, turb_raw = struct.unpack(
                ">HHHH", raw_bytes[9:17]
            )

            # 5. Apply scaling factors from instrument datasheet
            ph_value   = round(ph_raw   / 100.0, 2)   # e.g. 721  → 7.21
            temp_value = round(temp_raw / 100.0, 2)   # e.g. 2380 → 23.80
            cond_value = round(cond_raw / 10.0,  1)   # e.g. 4250 → 425.0
            turb_value = round(turb_raw / 100.0, 2)   # e.g. 15   → 0.15

            logger.debug(
                "AquaSense parsed: pH=%.2f temp=%.2f cond=%.1f turb=%.2f",
                ph_value, temp_value, cond_value, turb_value
            )

            # 6. Return standard InstruLog format
            return {
                "status": "success",
                "instrument_type": self.instrument_type,
                "transaction_id": transaction_id,
                "metrics": {
                    "pH": {
                        "value": ph_value,
                        "unit": "pH"
                    },
                    "temperature": {
                        "value": temp_value,
                        "unit": "Celsius"
                    },
                    "conductivity": {
                        "value": cond_value,
                        "unit": "µS/cm"
                    },
                    "turbidity": {
                        "value": turb_value,
                        "unit": "NTU"
                    }
                }
            }

        except struct.error as e:
            logger.error("AquaSense struct unpack error: %s | raw=%s", e, raw_bytes.hex())
            return {"status": "error", "message": f"Modbus parse error: {e}"}

        except Exception as e:
            logger.error("AquaSense unexpected error: %s", e)
            return {"status": "error", "message": f"Plugin error: {e}"}
```

---

## 13. FAQ

**Q: Can I have multiple classes in `integration.py`?**
Yes, but only one will be loaded — the first class found that has all three required members (`instrument_type`, `connect`, `parse_to_json`). Keep helper classes private by prefixing with `_`.

**Q: Can I import third-party libraries?**
Yes. If your plugin needs `pymodbus`, `aiohttp`, `numpy`, etc., add them to the backend's `requirements.txt` and they will be available. For hosted/downloaded plugins, document the dependencies clearly in your README.

**Q: What happens if my plugin crashes during streaming?**
The stream engine catches all exceptions inside the poll loop, increments the error counter, logs the error, and continues. After 5 consecutive errors it sends an alert email to the admin. The stream only stops if the TCP connection is closed by the instrument.

**Q: Can I use synchronous code?**
All methods must be `async`. If you need to call a synchronous library, wrap it with `asyncio.to_thread()`:
```python
result = await asyncio.to_thread(my_sync_function, arg1, arg2)
```

**Q: My instrument uses a different Modbus function code. What do I do?**
Handle it in your `parse_to_json()`. Check `raw_bytes[7]` for the function code and unpack accordingly. Function code `0x03` (Read Holding Registers) and `0x04` (Read Input Registers) are the most common.

**Q: Can my plugin maintain state between reads?**
Yes. Store state as instance attributes in `__init__`. The same plugin instance is reused for every poll cycle:
```python
def __init__(self):
    self._read_count = 0
    self._last_ph = None

async def parse_to_json(self, raw_bytes):
    self._read_count += 1
    ...
```

**Q: How do I handle instruments that send data in little-endian byte order?**
Replace `">"` (big-endian) with `"<"` (little-endian) in your `struct.unpack()` format string.

**Q: The `github_url` install keeps returning 404. What's wrong?**
Make sure you are using the **raw** URL, not the GitHub browser URL, and that it points to the **directory** not the file. See [section 9](#9-publishing-to-github). Also ensure the repo is **public** — private repos require authentication which the backend doesn't support.
