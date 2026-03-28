#!/usr/bin/env python3
"""
OrefAlert Pi Service — with full idle + alert color/brightness control.
Runs headless as a systemd service on Raspberry Pi Zero 2.
"""

import asyncio
import json
import logging
import os
import socket
import time
import requests
from collections import deque
from datetime import datetime
from typing import Optional

from bleak import BleakClient, BleakScanner

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
_LOG_DIR    = "/run/oref-alert" if os.path.isdir("/run/oref-alert") else "/tmp"
LOG_FILE    = os.path.join(_LOG_DIR, "oref_service.log")

# ─── systemd watchdog ─────────────────────────────────────────────────────────
_WATCHDOG_USEC     = int(os.environ.get("WATCHDOG_USEC", 0))
_WATCHDOG_INTERVAL = (_WATCHDOG_USEC / 1_000_000 / 2) if _WATCHDOG_USEC else 0

def _sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        sock.sendto(msg.encode(), addr)
        sock.close()
    except Exception:
        pass

def sd_ready():
    _sd_notify("READY=1")

def sd_watchdog():
    _sd_notify("WATCHDOG=1")

# ─── Shared log buffer ────────────────────────────────────────────────────────
log_buffer = deque(maxlen=200)

class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append({
            "ts":    self.formatTime(record, "%H:%M:%S"),
            "level": record.levelname,
            "msg":   record.getMessage(),
        })

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
        BufferHandler(),
    ],
)
log = logging.getLogger("oref")

# ─── Oref API ─────────────────────────────────────────────────────────────────
OREF_URL        = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
OREF_URL_BACKUP = "https://www.oref.org.il/warningMessages/alert/Alerts.json"
OREF_HEADERS    = {
    "Referer":          "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent":       "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36",
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":  "he-IL,he;q=0.9",
    "Cache-Control":    "no-cache",
    "Pragma":           "no-cache",
}

# ─── Alert categories ─────────────────────────────────────────────────────────
ALERT_DEFAULTS = {
    "1":   {"r": 255, "g": 0,   "b": 0,   "brightness": 100, "name": "ירי רקטות וטילים"},
    "2":   {"r": 255, "g": 0,   "b": 0,   "brightness": 100, "name": "ירי לא מזוהה"},
    "3":   {"r": 255, "g": 140, "b": 0,   "brightness": 100, "name": "חדירת כלי טיס עוין"},
    "4":   {"r": 0,   "g": 220, "b": 0,   "brightness": 100, "name": "חדירת מחבלים"},
    "5":   {"r": 128, "g": 0,   "b": 255, "brightness": 80,  "name": "רעידת אדמה"},
    "6":   {"r": 0,   "g": 255, "b": 100, "brightness": 80,  "name": "חומרים רדיואקטיביים"},
    "7":   {"r": 255, "g": 255, "b": 0,   "brightness": 80,  "name": "אירוע כימי"},
    "8":   {"r": 0,   "g": 100, "b": 255, "brightness": 80,  "name": "צונאמי"},
    "13":  {"r": 255, "g": 0,   "b": 0,   "brightness": 100, "name": "פיגוע"},
    "101": {"r": 0,   "g": 200, "b": 255, "brightness": 50,  "name": "תרגיל"},
}

FLASH_PATTERNS = {
    "fast_strobe":   [("on", 150), ("off", 100), ("on", 150), ("off", 100)],
    "medium_strobe": [("on", 250), ("off", 200)],
    "slow_pulse":    [("on", 600), ("off", 400)],
    "solid":         [("on", 2000)],
}

DEFAULT_PATTERN_MAP = {
    "1":   "fast_strobe",
    "2":   "fast_strobe",
    "3":   "medium_strobe",
    "4":   "medium_strobe",
    "5":   "slow_pulse",
    "6":   "slow_pulse",
    "7":   "slow_pulse",
    "8":   "slow_pulse",
    "13":  "fast_strobe",
    "101": "solid",
}

# ─── BLE ──────────────────────────────────────────────────────────────────────
BLE_CHAR_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"

def ble_color_cmd(r, g, b):
    return bytes([0x7e, 0x00, 0x05, 0x03,
                  max(0, min(255, r)),
                  max(0, min(255, g)),
                  max(0, min(255, b)),
                  0x00, 0xef])

def apply_brightness(r, g, b, pct):
    f = max(1, min(100, pct)) / 100
    return int(r * f), int(g * f), int(b * f)

DEFAULT_IDLE = {"r": 255, "g": 200, "b": 120, "brightness": 15, "on": True}

# ─── Config ───────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "ble_addresses":  [],
    "my_city":        "",
    "all_country":    False,
    "poll_interval":  0.5,
    "alert_duration": 3600,
    "ntfy_topic":     "",
    "ntfy_server":    "https://ntfy.sh",
    "pattern_map":    DEFAULT_PATTERN_MAP.copy(),
    "enabled_cats":   list(ALERT_DEFAULTS.keys()),
    "idle":           DEFAULT_IDLE.copy(),
    "alert_colors":   {},
}

_config_mtime = 0.0
_config = {}


def load_config():
    global _config, _config_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if mtime != _config_mtime:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            _config = {**DEFAULT_CONFIG, **loaded}
            _config_mtime = mtime
            log.info("Config reloaded")
    except FileNotFoundError:
        _config = DEFAULT_CONFIG.copy()
        save_config(_config)
    except Exception as e:
        log.warning("Config load error: %s", e)
        if not _config:
            _config = DEFAULT_CONFIG.copy()
    return _config


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_alert_color(cfg, cat):
    base = ALERT_DEFAULTS.get(cat, {"r": 255, "g": 50, "b": 0, "brightness": 100, "name": "התרעה"})
    override = cfg.get("alert_colors", {}).get(cat, {})
    return {**base, **override}


# ─── BLE Connection Manager ───────────────────────────────────────────────────
class BLEManager:

    def __init__(self):
        self.clients = {}
        self._lock = asyncio.Lock()
        self._flash_task = None
        self._stop_flash = asyncio.Event()
        self._alert_active = False

    async def maintain_loop(self):
        while True:
            cfg = load_config()
            addresses = cfg.get("ble_addresses", [])
            async with self._lock:
                for addr in [a for a in self.clients if a not in addresses]:
                    try:
                        await self.clients[addr].disconnect()
                    except Exception:
                        pass
                    del self.clients[addr]
                    log.info("BLE removed: %s", addr)

                for addr in addresses:
                    client = self.clients.get(addr)
                    if client is None or not client.is_connected:
                        try:
                            c = BleakClient(addr, timeout=10)
                            await c.connect()
                            self.clients[addr] = c
                            log.info("BLE connected: %s", addr)
                            if not self._alert_active:
                                await self._apply_idle_single(c, load_config())
                        except Exception as e:
                            log.warning("BLE connect failed %s: %s", addr, e)
            await asyncio.sleep(15)

    async def _write_all(self, cmd):
        async with self._lock:
            tasks = [
                client.write_gatt_char(BLE_CHAR_UUID, cmd, response=False)
                for client in self.clients.values()
                if client.is_connected
            ]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        log.warning("BLE write error: %s", result)

    async def _apply_idle_single(self, client, cfg):
        idle = cfg.get("idle", DEFAULT_IDLE)
        if not idle.get("on", True):
            cmd = ble_color_cmd(0, 0, 0)
        else:
            r, g, b = apply_brightness(
                idle["r"], idle["g"], idle["b"], idle.get("brightness", 15)
            )
            cmd = ble_color_cmd(r, g, b)
        try:
            await client.write_gatt_char(BLE_CHAR_UUID, cmd, response=False)
        except Exception as e:
            log.warning("Idle apply error: %s", e)

    async def apply_idle(self):
        cfg = load_config()
        idle = cfg.get("idle", DEFAULT_IDLE)
        if not idle.get("on", True):
            cmd = ble_color_cmd(0, 0, 0)
            state_str = "off"
        else:
            r, g, b = apply_brightness(
                idle["r"], idle["g"], idle["b"], idle.get("brightness", 15)
            )
            cmd = ble_color_cmd(r, g, b)
            state_str = "rgb({},{},{}) @ {}%".format(
                idle["r"], idle["g"], idle["b"], idle.get("brightness", 15)
            )
        await self._write_all(cmd)
        log.info("Idle state applied: %s", state_str)

    async def set_color(self, r, g, b, brightness):
        if not self._alert_active:
            ar, ag, ab = apply_brightness(r, g, b, brightness)
            await self._write_all(ble_color_cmd(ar, ag, ab))

    async def set_power(self, on):
        if not self._alert_active:
            if on:
                await self.apply_idle()
            else:
                await self._write_all(ble_color_cmd(0, 0, 0))

    async def flash(self, r, g, b, brightness, pattern_name):
        await self._stop_flash_task()
        self._alert_active = True
        self._stop_flash.clear()
        ar, ag, ab = apply_brightness(r, g, b, brightness)
        self._flash_task = asyncio.create_task(
            self._flash_loop(ar, ag, ab, pattern_name)
        )

    async def _flash_loop(self, r, g, b, pattern_name):
        pattern = FLASH_PATTERNS.get(pattern_name, FLASH_PATTERNS["medium_strobe"])
        try:
            while not self._stop_flash.is_set():
                for (state, ms) in pattern:
                    if self._stop_flash.is_set():
                        break
                    cmd = ble_color_cmd(r, g, b) if state == "on" else ble_color_cmd(0, 0, 0)
                    await self._write_all(cmd)
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._stop_flash.wait()),
                            timeout=ms / 1000,
                        )
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            pass
        finally:
            self._alert_active = False
            await self.apply_idle()

    async def _stop_flash_task(self):
        self._stop_flash.set()
        if self._flash_task and not self._flash_task.done():
            self._flash_task.cancel()
            try:
                await self._flash_task
            except asyncio.CancelledError:
                pass
        self._flash_task = None

    async def stop_alert(self):
        await self._stop_flash_task()
        self._alert_active = False
        await self.apply_idle()

    async def test_flash(self):
        await self.flash(255, 0, 0, 100, "fast_strobe")
        await asyncio.sleep(3)
        await self.stop_alert()

    @property
    def connected_count(self):
        return sum(1 for c in self.clients.values() if c.is_connected)


# ─── Push notification ────────────────────────────────────────────────────────
def push_notify(cfg, title, message, priority="high"):
    topic = cfg.get("ntfy_topic", "").strip()
    if not topic:
        return
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    try:
        requests.post(
            "{}/{}".format(server, topic),
            data=message.encode("utf-8"),
            headers={
                "Title":    title.encode("utf-8"),
                "Priority": priority,
                "Tags":     "rotating_light",
            },
            timeout=5,
        )
        log.info("Push sent: %s", title)
    except Exception as e:
        log.warning("Push failed: %s", e)


# ─── Oref polling ─────────────────────────────────────────────────────────────
def fetch_alerts():
    for url in [OREF_URL, OREF_URL_BACKUP]:
        try:
            resp = requests.get(url, headers=OREF_HEADERS, timeout=2)
            raw = resp.text.strip().lstrip("\ufeff").strip()
            if not raw or raw in ("", "[]", "{}"):
                return []
            data = json.loads(raw)
            if isinstance(data, dict) and "data" in data:
                return [data]
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def is_relevant(alert, cfg):
    if cfg.get("all_country") or not cfg.get("my_city", "").strip():
        return True
    city = cfg["my_city"].strip()
    return any(
        city == c.strip() or city in c.strip() or c.strip() in city
        for c in alert.get("data", [])
    )


# ─── Main service loop ────────────────────────────────────────────────────────
async def service_loop(ble):
    seen_ids = set()
    alert_active = False
    alert_end_time = 0.0

    log.info("Oref alert service started")

    while True:
        cfg = load_config()

        try:
            alerts = fetch_alerts()
        except Exception as e:
            log.debug("Fetch error: %s", e)
            await asyncio.sleep(cfg["poll_interval"])
            continue

        for alert in alerts:
            alert_id = alert.get("id", "")
            if not alert_id or alert_id in seen_ids:
                continue

            cat       = str(alert.get("cat", ""))
            cities    = alert.get("data", [])
            desc      = alert.get("desc", "")
            title_str = alert.get("title", "")

            if cat == "14":
                log.info("Early warning | %s", ", ".join(cities))
                seen_ids.add(alert_id)
                continue

            if cat == "10":
                log.info("All clear | %s", ", ".join(cities))
                seen_ids.add(alert_id)
                if alert_active:
                    alert_end_time = time.time() + 10
                continue

            seen_ids.add(alert_id)

            if not is_relevant(alert, cfg):
                log.info("Skipped (other city) | %s | %s", cat, ", ".join(cities))
                continue

            if cat not in cfg.get("enabled_cats", list(ALERT_DEFAULTS.keys())):
                log.info("Skipped (category disabled) | %s", cat)
                continue

            color      = get_alert_color(cfg, cat)
            pattern    = cfg["pattern_map"].get(cat, "medium_strobe")
            cities_str = ", ".join(cities)

            log.info("ALERT | %s | %s | %s", color["name"], cities_str, desc)

            if ble.connected_count > 0:
                await ble.flash(
                    color["r"], color["g"], color["b"],
                    color.get("brightness", 100),
                    pattern,
                )
            else:
                log.warning("No BLE bulbs connected")

            push_notify(
                cfg,
                title="ALERT {}".format(color["name"]),
                message="{}\n{}".format(cities_str, desc) if desc else cities_str,
            )

            alert_active   = True
            alert_end_time = time.time() + cfg.get("alert_duration", 3600)

        if alert_active and time.time() > alert_end_time:
            log.info("Alert expired - restoring idle")
            await ble.stop_alert()
            alert_active = False

        if len(seen_ids) > 500:
            seen_ids = set(list(seen_ids)[-200:])

        sd_watchdog()
        await asyncio.sleep(cfg["poll_interval"])


# ─── Entry point ──────────────────────────────────────────────────────────────
ble_manager = BLEManager()


async def main():
    load_config()
    log.info("Starting BLE connection manager...")
    sd_ready()
    asyncio.create_task(ble_manager.maintain_loop())
    await asyncio.sleep(2)
    await service_loop(ble_manager)


if __name__ == "__main__":
    asyncio.run(main())
