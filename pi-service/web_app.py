#!/usr/bin/env python3
"""
OrefAlert Web UI
Flask app serving the configuration interface and REST API.
Runs alongside oref_service.py as a separate systemd service.
"""

import asyncio
import json
import os
import queue
import threading
import time

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from bleak import BleakScanner

# Import shared state from the service module
import oref_service as svc

app = Flask(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# SSE subscriber queues - each connected browser gets its own queue
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()

# ─── Log buffer → SSE bridge ──────────────────────────────────────────────────
class SSELogHandler:
    """Watches log_buffer for new entries and pushes to SSE subscribers."""
    def __init__(self):
        self._last_len = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            buf = svc.log_buffer
            current_len = len(buf)
            if current_len > self._last_len:
                new_entries = list(buf)[self._last_len:]
                for entry in new_entries:
                    msg = json.dumps(entry)
                    with _sse_lock:
                        dead = []
                        for q in _sse_subscribers:
                            try:
                                q.put_nowait(msg)
                            except queue.Full:
                                dead.append(q)
                        for q in dead:
                            _sse_subscribers.remove(q)
                self._last_len = current_len
            time.sleep(0.3)

SSELogHandler()


# ─── Helper ───────────────────────────────────────────────────────────────────
def get_config() -> dict:
    return svc.load_config()

def write_config(data: dict):
    cfg = get_config()
    cfg.update(data)
    svc.save_config(cfg)
    svc.load_config()  # force immediate reload
    return cfg


# ─── Pages ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", config=get_config())


# ─── API: config ──────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(get_config())


@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.json or {}
    # Validate and sanitise key fields
    allowed = {
        "my_city", "all_country", "poll_interval",
        "ntfy_topic", "ntfy_server", "pattern_map",
        "enabled_cats", "alert_duration",
    }
    clean = {k: v for k, v in data.items() if k in allowed}
    cfg = write_config(clean)
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/config/bulbs", methods=["POST"])
def api_config_bulbs():
    """Add, remove, or reorder BLE bulb addresses."""
    data = request.json or {}
    addresses = data.get("ble_addresses", [])
    if not isinstance(addresses, list):
        return jsonify({"ok": False, "error": "ble_addresses must be a list"}), 400
    cfg = write_config({"ble_addresses": addresses})
    return jsonify({"ok": True, "ble_addresses": cfg["ble_addresses"]})


# ─── API: scan ────────────────────────────────────────────────────────────────
@app.route("/api/scan/ble", methods=["POST"])
def api_scan_ble():
    """Scan for nearby BLE devices. Returns list of {address, name}."""
    result = {"devices": [], "error": None}
    try:
        loop = asyncio.new_event_loop()
        devices = loop.run_until_complete(BleakScanner.discover(timeout=10))
        loop.close()
        result["devices"] = [
            {"address": d.address, "name": d.name or "Unknown"}
            for d in sorted(devices, key=lambda x: x.name or "")
        ]
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


# ─── API: BLE status ──────────────────────────────────────────────────────────
@app.route("/api/ble/status", methods=["GET"])
def api_ble_status():
    cfg = get_config()
    addresses = cfg.get("ble_addresses", [])
    status = []
    for addr in addresses:
        client = svc.ble_manager.clients.get(addr)
        status.append({
            "address":   addr,
            "connected": client.is_connected if client else False,
        })
    return jsonify({
        "bulbs":           status,
        "connected_count": svc.ble_manager.connected_count,
    })


# ─── API: test flash ──────────────────────────────────────────────────────────
@app.route("/api/test/flash", methods=["POST"])
def api_test_flash():
    data = request.json or {}
    addr = data.get("address")  # optional - if None, tests all bulbs

    async def _run():
        await svc.ble_manager.test_flash(addr)

    thread = threading.Thread(
        target=lambda: asyncio.run(_run()),
        daemon=True
    )
    thread.start()
    return jsonify({"ok": True})


# ─── API: test push ───────────────────────────────────────────────────────────
@app.route("/api/test/push", methods=["POST"])
def api_test_push():
    cfg = get_config()
    svc.push_notify(cfg, title="בדיקת התרעה", message="זוהי בדיקה של מערכת ההתרעות", priority="default")
    return jsonify({"ok": True})


# ─── API: service status ──────────────────────────────────────────────────────
@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "connected_bulbs": svc.ble_manager.connected_count,
        "log_entries":     len(svc.log_buffer),
    })


# ─── API: log history ────────────────────────────────────────────────────────
@app.route("/api/log", methods=["GET"])
def api_log():
    return jsonify(list(svc.log_buffer))


# ─── SSE: live log stream ─────────────────────────────────────────────────────
@app.route("/api/log/stream")
def api_log_stream():
    q: queue.Queue = queue.Queue(maxsize=100)
    with _sse_lock:
        _sse_subscribers.append(q)

    # Send existing buffer first
    def generate():
        for entry in list(svc.log_buffer):
            yield f"data: {json.dumps(entry)}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with _sse_lock:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False, threaded=True)


# ─── API: idle light control ──────────────────────────────────────────────────
@app.route("/api/light/idle", methods=["POST"])
def api_light_idle():
    """Set idle color+brightness and apply immediately to bulbs."""
    data = request.json or {}
    idle = {
        "r":          max(0, min(255, int(data.get("r", 255)))),
        "g":          max(0, min(255, int(data.get("g", 200)))),
        "b":          max(0, min(255, int(data.get("b", 120)))),
        "brightness": max(1, min(100, int(data.get("brightness", 15)))),
        "on":         bool(data.get("on", True)),
    }
    write_config({"idle": idle})

    async def _apply():
        await svc.ble_manager.apply_idle()
    threading.Thread(target=lambda: asyncio.run(_apply()), daemon=True).start()

    return jsonify({"ok": True, "idle": idle})


@app.route("/api/light/power", methods=["POST"])
def api_light_power():
    """Turn all bulbs on or off."""
    on = request.json.get("on", True)
    cfg = get_config()
    idle = cfg.get("idle", {})
    idle["on"] = bool(on)
    write_config({"idle": idle})

    async def _apply():
        await svc.ble_manager.set_power(on)
    threading.Thread(target=lambda: asyncio.run(_apply()), daemon=True).start()

    return jsonify({"ok": True, "on": on})


# ─── API: alert color overrides ───────────────────────────────────────────────
@app.route("/api/alert/colors", methods=["POST"])
def api_alert_colors():
    """Save per-category alert color+brightness overrides."""
    data = request.json or {}
    # data = { "1": {"r":255,"g":0,"b":0,"brightness":100}, ... }
    cfg = write_config({"alert_colors": data})
    return jsonify({"ok": True, "alert_colors": cfg.get("alert_colors", {})})
