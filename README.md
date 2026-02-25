<p align="center">
  <img src="docs/assets/logo.png" alt="Watch and Reboot Logo" width="200"/>
</p>

<p align="center">
  <a href="https://www.python.org/">
    <img src="https://img.shields.io/badge/Python-3.8%2B-blue" alt="Python Version">
  </a>
  <a href="https://opensource.org/licenses/MIT">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  </a>
</p>

# 🔃 Watch and Reboot 

Watch and Reboot is a lightweight Python script, watchdog and remediation tool designed for [**MeshMonitor**](https://github.com/Yeraze/MeshMonitor) and [**Meshstatic**](https://meshtastic.org/) based deployments.

It can:

- Watch a target device by IP (ping + optional SSH)
- Watch a Meshtastic node reached via a BLE bridge TCP proxy (e.g., Yeraze/meshtastic-ble-bridge)
- Automatically remediate by:
  - Restarting the BLE bridge (systemd or docker/compose)
  - Restarting MeshMonitor (systemd or docker/compose)
  - Rebooting the actual target device (via SSH + shutdown -r now)
- Emit “unhealthy”, “action”, and “recovered” events via:
  - MQTT (mosquitto_pub)
  - Webhook (curl)
  - Optional Meshtastic text message (via meshtastic Python library)

It is:

- Cron-friendly
- Capability-aware
- Cooldown-protected (prevents reboot storms)
- Self-contained (replaces prior MeshTools scripts)

---

# Project Lineage (MeshTools → Watch and Reboot)

This capability originally existed as an internal MeshTools script set.

v2.0 is the simplified public release:

- Single executable script
- No dependency on external management scripts
- Cleaner remediation order
- Safer defaults
- Easier to deploy across nodes

This tool replaces previous management helpers such as:

- meshmonitor-manage.sh
- meshtastic-manage.sh

Everything is now unified into:

meshmonitor-watchandreboot

---

# Quick Start

## 1) Install

Copy the script to:

/usr/local/sbin/meshmonitor-watchandreboot

Then make it executable:

sudo chmod +x /usr/local/sbin/meshmonitor-watchandreboot

---

## 2) Check Capabilities

meshmonitor-watchandreboot status

---

## 3) Run a One-Time Health Check

IP mode:

meshmonitor-watchandreboot watch-once 10.0.0.10 --mode ip --user pi

BLE bridge mode:

meshmonitor-watchandreboot watch-once bridge-host --mode ble --ble-bridge-host 127.0.0.1 --ble-bridge-port 4403

---

# Modes

## Mode: IP (default)

Health check:
- ping
- ssh

Default remediation order:
1. Restart MeshMonitor (optional)
2. Reboot device (if allowed)

Use this for:
- Raspberry Pi nodes
- SDR feeders
- Remote Linux boxes
- Field repeaters

---

## Mode: BLE (Bridge-Based)

Health check:
- TCP connect to bridge_host:port (default 127.0.0.1:4403)

Default remediation order:
1. Restart BLE bridge
2. Restart MeshMonitor
3. Reboot host

Use this for:
- Meshtastic nodes accessed via BLE bridge
- USB-attached radios
- Adapter instability recovery

---

# Cron Usage

Run every 5 minutes:

*/5 * * * * /usr/local/sbin/meshmonitor-watchandreboot watch-once 10.0.0.10 --mode ip --user pi

Watch BLE bridge every 2 minutes:

*/2 * * * * /usr/local/sbin/meshmonitor-watchandreboot watch-once bridge-host --mode ble --ble-bridge-restart-mode local

---

# Configuration (Environment Variables)

All configuration can be done via environment variables.

---

## Core

MM_MODE=ip|ble  
MM_TARGET_HOST=10.0.0.10  
MM_REMOTE_USER=pi  
MM_SSH_PORT=22  

MM_FAIL_THRESHOLD=3  
MM_COOLDOWN_SECONDS=1800  
MM_RECOVERY_TIMEOUT=600  
MM_CHECK_INTERVAL=20  

---

## IP Mode Requirements

MM_REQUIRE_PING=1  
MM_REQUIRE_SSH=1  

---

## MeshMonitor Restart (Optional)

MM_RESTART_MODE=local|remote|off  
MM_SYSTEMD_SERVICE=meshmonitor  
MM_COMPOSE_DIR=/opt/meshmonitor  
MM_CONTAINER_NAME=meshmonitor  

---

## BLE Bridge (Yeraze/meshtastic-ble-bridge)

MM_BLE_BRIDGE_HOST=127.0.0.1  
MM_BLE_BRIDGE_PORT=4403  

MM_BLE_BRIDGE_RESTART_MODE=local|remote|off  
MM_BLE_BRIDGE_SYSTEMD_SERVICE=  
MM_BLE_BRIDGE_COMPOSE_DIR=/opt/meshmonitor  
MM_BLE_BRIDGE_CONTAINER_NAME=meshmonitor-ble-bridge  

---

## Reboot Control

MM_ALLOW_REBOOT=1  

Set to 0 if you want restart-only behavior.

---

# Notifications

## MQTT

MM_MQTT_HOST=10.0.0.5  
MM_MQTT_PORT=1883  
MM_MQTT_TOPIC_PREFIX=meshwatch  

---

## Webhook

MM_WEBHOOK_URL=https://example.com/webhook  

---

## Custom Shell Hook

MM_NOTIFY_CMD=echo "{EVENT} {HOST} {DETAILS}"

---

# Optional Meshtastic “Recovered” Message

Requires Python meshtastic library installed.

MM_MESHTASTIC_NOTIFY=1  
MM_MESHTASTIC_TEXT=Recovered: {HOST} is back online  
MM_MESHTASTIC_TCP_HOST=127.0.0.1  
MM_MESHTASTIC_TCP_PORT=4403  
MM_MESHTASTIC_SERIAL=/dev/ttyUSB0  
MM_MESHTASTIC_DEST=  

If MM_MESHTASTIC_DEST is empty, it broadcasts.

---

# Safety Notes

This tool can:

- Restart services
- Restart containers
- Execute remote SSH commands
- Reboot devices

Best practices:

- Use SSH keys (BatchMode)
- Restrict sudo permissions
- Keep cooldown enabled
- Avoid very low fail thresholds
- Prefer private/VPN networks

Cooldown prevents reboot storms.

---

# Design Philosophy

- KISS (Keep It Simple)
- Capability-aware
- Infrastructure-safe defaults
- Replace multiple scripts with one unified tool
- MeshMonitor-native thinking

---

# Logs & State

Default locations:

State: /var/tmp/meshmonitor-watchandreboot  
Log:   /var/log/meshmonitor-watchandreboot.log  

Falls back to state directory if /var/log is not writable.

---

## License

This project is licensed under the MIT License.

See the LICENSE file for details.  
Full license text: https://opensource.org/licenses/MIT

---

## Contributing

Pull requests are welcome. Open an issue first to discuss ideas or report bugs.

---

## Acknowledgments

* MeshMonitor built by [Yeraze](https://github.com/Yeraze) 

Discover other community-contributed scripts for MeshMonitor: https://meshmonitor.org/user-scripts.html
