#!/bin/bash
# OrefAlert Pi install script
# Run as root on a fresh Raspberry Pi OS Lite (64-bit)
# Usage: sudo bash install.sh

set -e

echo "=== OrefAlert Pi Installer ==="

APP_DIR=/opt/oref-alert

# ─── System packages ──────────────────────────────────────────────────────────
echo "[1/8] Installing system dependencies..."
apt-get update -q
apt-get install -y -q \
  python3 python3-pip python3-venv \
  bluetooth bluez \
  avahi-daemon \
  watchdog \
  git

systemctl enable bluetooth   && systemctl start bluetooth
systemctl enable avahi-daemon && systemctl start avahi-daemon

# ─── Hardware watchdog ────────────────────────────────────────────────────────
echo "[2/8] Enabling hardware watchdog..."

if ! grep -q "dtparam=watchdog=on" /boot/firmware/config.txt 2>/dev/null; then
  echo "dtparam=watchdog=on" >> /boot/firmware/config.txt
fi

cat > /etc/watchdog.conf << 'EOF'
watchdog-device = /dev/watchdog
watchdog-timeout = 15
interval = 5
max-load-1 = 24
min-memory = 1
EOF

systemctl enable watchdog
systemctl start watchdog || true

# ─── RAM-based logging (protect SD card) ─────────────────────────────────────
echo "[3/8] Configuring RAM logging..."

if ! grep -q "tmpfs /var/log" /etc/fstab; then
  echo "tmpfs /var/log tmpfs defaults,noatime,nosuid,mode=0755,size=32m 0 0" >> /etc/fstab
fi

mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/volatile.conf << 'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF

if ! grep -q "noatime" /etc/fstab; then
  sed -i 's/defaults/defaults,noatime/' /etc/fstab
fi

# ─── WiFi keepalive ───────────────────────────────────────────────────────────
echo "[4/8] Setting up WiFi keepalive..."

# Create dir first (missing on newer Pi OS)
mkdir -p /etc/network/interfaces.d
cat > /etc/network/interfaces.d/wlan0-nosleep << 'EOF'
iface wlan0 inet manual
  wireless-power off
EOF

# NetworkManager method (newer Pi OS)
if command -v nmcli &>/dev/null; then
  CONN=$(nmcli -t -f NAME con show --active 2>/dev/null | head -1)
  if [ -n "$CONN" ]; then
    nmcli con modify "$CONN" 802-11-wireless.powersave 2 2>/dev/null || true
  fi
fi

# rc.local method (older Pi OS)
if [ -f /etc/rc.local ]; then
  if ! grep -q "wireless-power off" /etc/rc.local 2>/dev/null; then
    sed -i 's/^exit 0/iwconfig wlan0 power off 2>\/dev\/null || true
exit 0/' /etc/rc.local
  fi
fi

# systemd service method (works on all versions)
cat > /etc/systemd/system/wifi-powersave-off.service << 'EOF'
[Unit]
Description=Disable WiFi power saving
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/iwconfig wlan0 power off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl enable wifi-powersave-off 2>/dev/null || true

cat > /usr/local/bin/wifi-keepalive.sh << 'EOF'
#!/bin/bash
GATEWAY=$(ip route | grep default | awk '{print $3}' | head -1)
[ -z "$GATEWAY" ] && GATEWAY="8.8.8.8"
if ! ping -c 2 -W 3 "$GATEWAY" > /dev/null 2>&1; then
  logger "wifi-keepalive: ping failed, reconnecting..."
  ip link set wlan0 down && sleep 2 && ip link set wlan0 up && sleep 5
  if ! ping -c 2 -W 3 "$GATEWAY" > /dev/null 2>&1; then
    logger "wifi-keepalive: still dead, rebooting"
    reboot
  fi
fi
EOF
chmod +x /usr/local/bin/wifi-keepalive.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/wifi-keepalive.sh") | crontab -

# ─── App user ─────────────────────────────────────────────────────────────────
echo "[5/8] Creating app user..."
id -u oref &>/dev/null || useradd -r -s /bin/false oref
usermod -aG bluetooth oref

# ─── Install app ──────────────────────────────────────────────────────────────
echo "[6/8] Installing app files..."
mkdir -p $APP_DIR
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp -r "$SCRIPT_DIR"/* $APP_DIR/
chown -R oref:oref $APP_DIR

# ─── Python virtualenv ────────────────────────────────────────────────────────
echo "[7/8] Setting up Python environment..."
python3 -m venv $APP_DIR/venv
$APP_DIR/venv/bin/pip install --upgrade pip -q
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt -q

# ─── Default config ───────────────────────────────────────────────────────────
if [ ! -f $APP_DIR/config.json ]; then
  cat > $APP_DIR/config.json << 'EOF'
{
  "ble_addresses": [],
  "my_city": "",
  "all_country": false,
  "poll_interval": 0.5,
  "alert_duration": 45,
  "ntfy_topic": "",
  "ntfy_server": "https://ntfy.sh",
  "pattern_map": {
    "1":   "fast_strobe",
    "2":   "fast_strobe",
    "3":   "medium_strobe",
    "4":   "medium_strobe",
    "5":   "slow_pulse",
    "6":   "slow_pulse",
    "7":   "slow_pulse",
    "8":   "slow_pulse",
    "13":  "fast_strobe",
    "101": "solid"
  },
  "enabled_cats": ["1","2","3","4","5","6","7","8","13","101"]
}
EOF
  chown oref:oref $APP_DIR/config.json
fi

hostnamectl set-hostname oref-alert

# ─── systemd services ─────────────────────────────────────────────────────────
echo "[8/8] Installing systemd services..."
cp $APP_DIR/oref-alert.service     /etc/systemd/system/
cp $APP_DIR/oref-alert-web.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable oref-alert oref-alert-web
systemctl start  oref-alert oref-alert-web

echo ""
echo "============================================"
echo " OrefAlert installed!"
echo "============================================"
echo ""
echo " Open http://{user}.local in your browser"
echo ""
echo " IMPORTANT: reboot to activate watchdog + RAM logging"
echo " Run: sudo reboot"
echo ""
