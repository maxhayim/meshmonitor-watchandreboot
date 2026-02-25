#!/usr/bin/env python3
# mm_meta:
#   name: Watch and Reboot
#   emoji: 🔃
#   language: Python

"""
meshmonitor-watchandreboot

History / lineage:
- Originally this capability lived as an internal "MeshTools" set of scripts.
- "Watch and Reboot" (v2.0) is the simplified public release: a single, complete tool that
  replaces the prior external script set (e.g., meshmonitor-manage.sh, meshtastic-manage.sh).
- Goal: capability-aware, cron-friendly remediation across IP targets and BLE-bridge targets.

What it does:
- Watches a target and determines "not working" based on mode:
  - mode=ip (default): ping + optional ssh reachability
  - mode=ble: TCP reachability to BLE bridge endpoint (default 127.0.0.1:4403)
- Remediates with safe escalation + storm controls:
  1) Restart bridge (when mode=ble; optional in ip if you set it)
  2) Restart MeshMonitor instance (optional)
  3) Reboot the actual host device (optional)
  4) Wait for recovery, then notify "recovered"

Notifications:
- MQTT (mosquitto_pub) if configured
- Webhook (curl) if configured
- Custom shell command hook if configured
- Optional Meshtastic text message via python "meshtastic" library (no external scripts)

Designed for cron:
- Tracks consecutive failures and cooldown in state files under MM_STATE_DIR
- Cooldown prevents reboot storms

Exit codes:
  0 = healthy OR remediated and recovered
  1 = unhealthy (below threshold) OR remediation attempted but recovery failed/timed out
  2 = usage/config error
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

ENV = os.environ

# ----------------------------
# Defaults (override via env)
# ----------------------------

DEFAULT_MODE = ENV.get("MM_MODE", "ip")  # ip | ble
DEFAULT_TARGET_HOST = ENV.get("MM_TARGET_HOST", "")

DEFAULT_REMOTE_USER = ENV.get("MM_REMOTE_USER", "pi")
DEFAULT_SSH_PORT = int(ENV.get("MM_SSH_PORT", "22"))

DEFAULT_FAIL_THRESHOLD = int(ENV.get("MM_FAIL_THRESHOLD", "3"))
DEFAULT_COOLDOWN_SECONDS = int(ENV.get("MM_COOLDOWN_SECONDS", "1800"))   # 30 min
DEFAULT_RECOVERY_TIMEOUT = int(ENV.get("MM_RECOVERY_TIMEOUT", "600"))    # 10 min
DEFAULT_CHECK_INTERVAL = int(ENV.get("MM_CHECK_INTERVAL", "20"))         # seconds

# ip-mode requirements
DEFAULT_REQUIRE_PING = ENV.get("MM_REQUIRE_PING", "1") == "1"
DEFAULT_REQUIRE_SSH = ENV.get("MM_REQUIRE_SSH", "1") == "1"

# MeshMonitor restart (optional)
DEFAULT_MM_RESTART_MODE = ENV.get("MM_RESTART_MODE", "local")  # local|remote|off
DEFAULT_MM_SYSTEMD_SERVICE = ENV.get("MM_SYSTEMD_SERVICE", "meshmonitor")
DEFAULT_MM_COMPOSE_DIR = ENV.get("MM_COMPOSE_DIR", "/opt/meshmonitor")
DEFAULT_MM_CONTAINER_NAME = ENV.get("MM_CONTAINER_NAME", "meshmonitor")

# BLE bridge (Yeraze/meshtastic-ble-bridge) defaults
DEFAULT_BLE_BRIDGE_HOST = ENV.get("MM_BLE_BRIDGE_HOST", "127.0.0.1")
DEFAULT_BLE_BRIDGE_PORT = int(ENV.get("MM_BLE_BRIDGE_PORT", "4403"))
DEFAULT_BLE_BRIDGE_RESTART_MODE = ENV.get("MM_BLE_BRIDGE_RESTART_MODE", "local")  # local|remote|off
DEFAULT_BLE_BRIDGE_SYSTEMD_SERVICE = ENV.get("MM_BLE_BRIDGE_SYSTEMD_SERVICE", "")
DEFAULT_BLE_BRIDGE_COMPOSE_DIR = ENV.get("MM_BLE_BRIDGE_COMPOSE_DIR", DEFAULT_MM_COMPOSE_DIR)
DEFAULT_BLE_BRIDGE_CONTAINER_NAME = ENV.get("MM_BLE_BRIDGE_CONTAINER_NAME", "meshmonitor-ble-bridge")

# Reboot behavior (optional)
DEFAULT_ALLOW_REBOOT = ENV.get("MM_ALLOW_REBOOT", "1") == "1"

# Notifications (optional)
DEFAULT_MQTT_HOST = ENV.get("MM_MQTT_HOST", "")
DEFAULT_MQTT_PORT = int(ENV.get("MM_MQTT_PORT", "1883"))
DEFAULT_MQTT_TOPIC_PREFIX = ENV.get("MM_MQTT_TOPIC_PREFIX", "meshwatch")

DEFAULT_WEBHOOK_URL = ENV.get("MM_WEBHOOK_URL", "")
DEFAULT_NOTIFY_CMD = ENV.get("MM_NOTIFY_CMD", "")  # {EVENT} {HOST} {USER} {DETAILS}

# Optional Meshtastic notify (no external scripts)
DEFAULT_MESHTASTIC_NOTIFY = ENV.get("MM_MESHTASTIC_NOTIFY", "0") == "1"
DEFAULT_MESHTASTIC_TEXT = ENV.get("MM_MESHTASTIC_TEXT", "Recovered: {HOST} is back online")
DEFAULT_MESHTASTIC_TCP_HOST = ENV.get("MM_MESHTASTIC_TCP_HOST", DEFAULT_BLE_BRIDGE_HOST)
DEFAULT_MESHTASTIC_TCP_PORT = int(ENV.get("MM_MESHTASTIC_TCP_PORT", str(DEFAULT_BLE_BRIDGE_PORT)))
DEFAULT_MESHTASTIC_SERIAL = ENV.get("MM_MESHTASTIC_SERIAL", "")
DEFAULT_MESHTASTIC_DEST = ENV.get("MM_MESHTASTIC_DEST", "")

# State/log
DEFAULT_STATE_DIR = ENV.get("MM_STATE_DIR", "/var/tmp/meshmonitor-watchandreboot")
DEFAULT_LOG_FILE = ENV.get("MM_LOG_FILE", "/var/log/meshmonitor-watchandreboot.log")


# ----------------------------
# Utilities
# ----------------------------

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def local_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def run(cmd: list[str], timeout: int = 20, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        timeout=timeout,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def which(bin_name: str) -> bool:
    return subprocess.call(["bash", "-lc", f"command -v {shlex.quote(bin_name)} >/dev/null 2>&1"]) == 0


def safe_slug(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in s)


def ensure_paths(state_dir: Path, log_file: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.touch(exist_ok=True)
        return log_file
    except PermissionError:
        fallback = state_dir / "meshmonitor-watchandreboot.log"
        fallback.touch(exist_ok=True)
        return fallback


def append_log(log_file: Path, msg: str) -> None:
    line = f"{now_iso()} | {msg}\n"
    try:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        sys.stderr.write(line)


def render_template(cmd_template: str, event: str, host: str, user: str, details: str) -> str:
    return (cmd_template
            .replace("{EVENT}", event)
            .replace("{HOST}", host)
            .replace("{USER}", user)
            .replace("{DETAILS}", details))


def render_text(template: str, host: str) -> str:
    return template.replace("{HOST}", host)


# ----------------------------
# Notifications
# ----------------------------

@dataclass
class Notifier:
    mqtt_host: str
    mqtt_port: int
    mqtt_topic_prefix: str
    webhook_url: str
    notify_cmd: str
    log_file: Path

    def notify(self, event: str, host: str, user: str, details: str) -> None:
        payload = {
            "event": event,
            "host": host,
            "user": user,
            "details": details,
            "at": now_iso(),
            "from": local_hostname(),
        }
        if self.mqtt_host:
            self._notify_mqtt(event, host, payload)
        if self.webhook_url:
            self._notify_webhook(payload)
        if self.notify_cmd:
            self._notify_cmd(event, host, user, details)

    def _notify_mqtt(self, event: str, host: str, payload: dict) -> None:
        if not which("mosquitto_pub"):
            append_log(self.log_file, "WARN: MQTT enabled but mosquitto_pub not found; skipping MQTT notify")
            return
        topic = f"{self.mqtt_topic_prefix}/{event}/{host}"
        msg = json.dumps(payload, separators=(",", ":"))
        try:
            run(["mosquitto_pub", "-h", self.mqtt_host, "-p", str(self.mqtt_port),
                 "-t", topic, "-m", msg], timeout=8, check=False)
        except Exception as e:
            append_log(self.log_file, f"WARN: MQTT publish failed: {e}")

    def _notify_webhook(self, payload: dict) -> None:
        if not which("curl"):
            append_log(self.log_file, "WARN: WEBHOOK enabled but curl not found; skipping webhook")
            return
        data = json.dumps(payload)
        try:
            run(["curl", "-sS", "-m", "8", "-H", "Content-Type: application/json",
                 "-X", "POST", self.webhook_url, "-d", data], timeout=10, check=False)
        except Exception as e:
            append_log(self.log_file, f"WARN: webhook failed: {e}")

    def _notify_cmd(self, event: str, host: str, user: str, details: str) -> None:
        cmd = render_template(self.notify_cmd, event, host, user, details)
        try:
            run(["bash", "-lc", cmd], timeout=15, check=False)
        except Exception as e:
            append_log(self.log_file, f"WARN: notify command failed: {e}")


# ----------------------------
# Meshtastic notify (optional, no external scripts)
# ----------------------------

def meshtastic_send_text(
    log_file: Path,
    text: str,
    tcp_host: str,
    tcp_port: int,
    serial_path: str,
    dest: str
) -> bool:
    try:
        from meshtastic.tcp_interface import TCPInterface
        from meshtastic.serial_interface import SerialInterface
    except Exception as e:
        append_log(log_file, f"WARN: meshtastic python lib not available; cannot send meshtastic msg ({e})")
        return False

    iface = None
    try:
        if tcp_host:
            iface = TCPInterface(hostname=tcp_host, portNumber=tcp_port)
        elif serial_path:
            iface = SerialInterface(devPath=serial_path)
        else:
            append_log(log_file, "WARN: meshtastic notify enabled but no tcp_host or serial_path provided")
            return False

        kwargs = {}
        if dest:
            kwargs["destinationId"] = dest

        iface.sendText(text, **kwargs)  # type: ignore[arg-type]
        append_log(log_file, f"MESHTASTIC: sent text (dest={dest or 'broadcast'})")
        return True
    except Exception as e:
        append_log(log_file, f"WARN: meshtastic send failed: {e}")
        return False
    finally:
        try:
            if iface:
                iface.close()
        except Exception:
            pass


# ----------------------------
# Health checks
# ----------------------------

def check_ping(host: str) -> bool:
    if not which("ping"):
        return False
    try:
        cp = run(["ping", "-c", "1", "-W", "2", host], timeout=5, check=False)
        return cp.returncode == 0
    except Exception:
        return False


def check_ssh(host: str, user: str, port: int) -> bool:
    if not which("ssh"):
        return False
    try:
        cp = run([
            "ssh", "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{user}@{host}",
            "echo", "ok"
        ], timeout=8, check=False)
        return cp.returncode == 0
    except Exception:
        return False


def check_tcp(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:
        return False


# ----------------------------
# Restart helpers (no external scripts)
# ----------------------------

def remote_cmd(host: str, user: str, port: int, command: str, timeout: int = 35) -> Tuple[bool, str]:
    if not which("ssh"):
        return False, "ssh not found"
    try:
        cp = run([
            "ssh",
            "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{user}@{host}",
            command
        ], timeout=timeout, check=False)
        out = (cp.stdout or "") + (cp.stderr or "")
        return cp.returncode == 0, out.strip()
    except Exception as e:
        return False, str(e)


def systemd_restart_local(service: str) -> bool:
    if not service or not which("systemctl"):
        return False
    exists = run(["bash", "-lc", f"systemctl list-unit-files | awk '{{print $1}}' | grep -qx {shlex.quote(service)}.service"],
                 timeout=8, check=False).returncode == 0
    if not exists:
        return False
    cp = run(["sudo", "systemctl", "restart", f"{service}.service"], timeout=30, check=False)
    return cp.returncode == 0


def systemd_restart_remote(host: str, user: str, port: int, service: str) -> bool:
    if not service:
        return False
    ok, _ = remote_cmd(host, user, port, f"sudo systemctl restart {shlex.quote(service)}.service", timeout=30)
    return ok


def compose_or_docker_restart_local(compose_dir: str, container: str) -> bool:
    if which("docker"):
        cpv = run(["docker", "compose", "version"], timeout=8, check=False)
        if cpv.returncode == 0:
            cp = run(["bash", "-lc", f"cd {shlex.quote(compose_dir)} 2>/dev/null && sudo docker compose restart {shlex.quote(container)}"],
                     timeout=45, check=False)
            if cp.returncode == 0:
                return True

        if which("docker-compose"):
            cp = run(["bash", "-lc", f"cd {shlex.quote(compose_dir)} 2>/dev/null && sudo docker-compose restart {shlex.quote(container)}"],
                     timeout=45, check=False)
            if cp.returncode == 0:
                return True

        cp = run(["bash", "-lc", f"sudo docker restart {shlex.quote(container)}"], timeout=30, check=False)
        return cp.returncode == 0
    return False


def compose_or_docker_restart_remote(host: str, user: str, port: int, compose_dir: str, container: str) -> bool:
    compose_dir_q = shlex.quote(compose_dir)
    container_q = shlex.quote(container)
    cmd = (
        f"cd {compose_dir_q} 2>/dev/null && "
        f"(sudo docker compose restart {container_q} || sudo docker-compose restart {container_q} || sudo docker restart {container_q})"
    )
    ok, _ = remote_cmd(host, user, port, cmd, timeout=60)
    return ok


def restart_component_local(systemd_service: str, compose_dir: str, container: str) -> bool:
    if systemd_service and systemd_restart_local(systemd_service):
        return True
    return compose_or_docker_restart_local(compose_dir, container)


def restart_component_remote(host: str, user: str, port: int, systemd_service: str, compose_dir: str, container: str) -> bool:
    if systemd_service and systemd_restart_remote(host, user, port, systemd_service):
        return True
    return compose_or_docker_restart_remote(host, user, port, compose_dir, container)


def reboot_device_remote(host: str, user: str, port: int) -> bool:
    ok, out = remote_cmd(host, user, port, "sudo /sbin/shutdown -r now", timeout=20)
    if ok:
        return True
    lowered = (out or "").lower()
    return ("connection" in lowered and ("closed" in lowered or "reset" in lowered))


# ----------------------------
# Recovery waits
# ----------------------------

def wait_for_recovery_ip(host: str, user: str, port: int, timeout_s: int, interval_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if check_ping(host) and check_ssh(host, user, port):
            return True
        time.sleep(interval_s)
    return False


def wait_for_recovery_ble(bridge_host: str, bridge_port: int, timeout_s: int, interval_s: int) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if check_tcp(bridge_host, bridge_port, timeout_s=2.5):
            return True
        time.sleep(interval_s)
    return False


# ----------------------------
# Watch logic
# ----------------------------

@dataclass
class WatchConfig:
    mode: str  # ip|ble
    target_host: str
    ssh_user: str
    ssh_port: int

    fail_threshold: int
    cooldown_seconds: int
    recovery_timeout: int
    check_interval: int

    require_ping: bool
    require_ssh: bool

    # bridge
    ble_bridge_host: str
    ble_bridge_port: int
    ble_bridge_restart_mode: str  # local|remote|off
    ble_bridge_systemd_service: str
    ble_bridge_compose_dir: str
    ble_bridge_container: str

    # meshmonitor
    mm_restart_mode: str  # local|remote|off
    mm_systemd_service: str
    mm_compose_dir: str
    mm_container: str

    allow_reboot: bool

    # meshtastic notify
    meshtastic_notify: bool
    meshtastic_text: str
    meshtastic_tcp_host: str
    meshtastic_tcp_port: int
    meshtastic_serial: str
    meshtastic_dest: str


def state_paths(state_dir: Path, cfg: WatchConfig) -> Tuple[Path, Path]:
    key = safe_slug(f"{cfg.mode}_{cfg.target_host}_{cfg.ssh_user}_{cfg.ssh_port}_{cfg.ble_bridge_host}_{cfg.ble_bridge_port}")
    return state_dir / f"{key}.fails", state_dir / f"{key}.cooldown"


def read_int(path: Path, default: int = 0) -> int:
    try:
        if path.exists():
            return int(path.read_text().strip() or str(default))
    except Exception:
        pass
    return default


def write_int(path: Path, value: int) -> None:
    path.write_text(str(value))


def watch_once(cfg: WatchConfig, state_dir: Path, log_file: Path, notify: Notifier) -> int:
    fails_file, cooldown_file = state_paths(state_dir, cfg)
    fails = read_int(fails_file, 0)

    # ----------------------------
    # Health evaluation
    # ----------------------------
    if cfg.mode == "ip":
        ping_ok = check_ping(cfg.target_host)
        ssh_ok = check_ssh(cfg.target_host, cfg.ssh_user, cfg.ssh_port)

        required_ok = True
        if cfg.require_ping and not ping_ok:
            required_ok = False
        if cfg.require_ssh and not ssh_ok:
            required_ok = False

        if required_ok:
            write_int(fails_file, 0)
            append_log(log_file, f"HEALTHY(ip): {cfg.target_host} ping={ping_ok} ssh={ssh_ok}")
            return 0

        fails += 1
        write_int(fails_file, fails)
        details = f"fail={fails}/{cfg.fail_threshold} ping={ping_ok} ssh={ssh_ok}"
        append_log(log_file, f"UNHEALTHY(ip): {cfg.target_host} {details}")
        notify.notify("unhealthy", cfg.target_host, cfg.ssh_user, details)

    elif cfg.mode == "ble":
        bridge_ok = check_tcp(cfg.ble_bridge_host, cfg.ble_bridge_port, timeout_s=2.0)

        if bridge_ok:
            write_int(fails_file, 0)
            append_log(log_file, f"HEALTHY(ble): {cfg.ble_bridge_host}:{cfg.ble_bridge_port}")
            return 0

        fails += 1
        write_int(fails_file, fails)
        details = f"fail={fails}/{cfg.fail_threshold} tcp={bridge_ok} bridge={cfg.ble_bridge_host}:{cfg.ble_bridge_port}"
        append_log(log_file, f"UNHEALTHY(ble): {cfg.target_host} {details}")
        notify.notify("unhealthy", cfg.target_host, cfg.ssh_user, details)

    else:
        append_log(log_file, f"ERROR: unknown mode {cfg.mode}")
        return 2

    if fails < cfg.fail_threshold:
        return 1

    # ----------------------------
    # Cooldown gate
    # ----------------------------
    now = int(time.time())
    last = read_int(cooldown_file, 0)

    if now - last < cfg.cooldown_seconds:
        append_log(log_file, f"COOLDOWN: {cfg.target_host} skipping remediation (cooldown={cfg.cooldown_seconds}s)")
        notify.notify("cooldown", cfg.target_host, cfg.ssh_user, "cooldown active; skipping remediation")
        write_int(fails_file, 0)
        return 1

    # storm protection
    write_int(cooldown_file, now)
    write_int(fails_file, 0)

    notify.notify("action/start", cfg.target_host, cfg.ssh_user, f"threshold reached; mode={cfg.mode}")
    append_log(log_file, f"ACTION: threshold reached for {cfg.target_host} mode={cfg.mode}")

    # ----------------------------
    # Remediation order (opinionated default)
    # ----------------------------
    # In BLE mode, the most common failure is the bridge layer (adapter/daemon/container).
    # Therefore: bridge -> meshmonitor -> reboot (optional).
    #
    # In IP mode, the "device not working" is usually reachability. If SSH is down and ping is down,
    # a service restart can't help. Therefore: meshmonitor (optional local) -> reboot (if allowed).
    # You can always force bridge actions in ip mode by running mode=ble for that host/bridge.

    # 1) Restart bridge (BLE mode)
    if cfg.mode == "ble" and cfg.ble_bridge_restart_mode != "off":
        notify.notify(
            "action/restart-bridge",
            cfg.target_host,
            cfg.ssh_user,
            f"restart_mode={cfg.ble_bridge_restart_mode} svc='{cfg.ble_bridge_systemd_service}' container='{cfg.ble_bridge_container}'"
        )
        append_log(log_file, "REMEDIATE: restarting BLE bridge")

        ok = False
        if cfg.ble_bridge_restart_mode == "local":
            ok = restart_component_local(cfg.ble_bridge_systemd_service, cfg.ble_bridge_compose_dir, cfg.ble_bridge_container)
        elif cfg.ble_bridge_restart_mode == "remote":
            ok = restart_component_remote(
                cfg.target_host,
                cfg.ssh_user,
                cfg.ssh_port,
                cfg.ble_bridge_systemd_service,
                cfg.ble_bridge_compose_dir,
                cfg.ble_bridge_container
            )
        append_log(log_file, f"BRIDGE-RESTART: ok={ok}")

        time.sleep(5)
        if wait_for_recovery_ble(cfg.ble_bridge_host, cfg.ble_bridge_port, timeout_s=60, interval_s=5):
            notify.notify("fixed/bridge", cfg.target_host, cfg.ssh_user, "bridge recovered after restart")
            append_log(log_file, "FIXED: bridge recovered (no reboot needed)")
            return 0

    # 2) Restart MeshMonitor (optional)
    if cfg.mm_restart_mode != "off":
        notify.notify("action/restart-meshmonitor", cfg.target_host, cfg.ssh_user, f"restart_mode={cfg.mm_restart_mode}")
        append_log(log_file, "REMEDIATE: restarting MeshMonitor")

        mm_ok = False
        if cfg.mm_restart_mode == "local":
            mm_ok = restart_component_local(cfg.mm_systemd_service, cfg.mm_compose_dir, cfg.mm_container)
        elif cfg.mm_restart_mode == "remote":
            # only if SSH is up; otherwise skip and escalate
            if check_ssh(cfg.target_host, cfg.ssh_user, cfg.ssh_port):
                mm_ok = restart_component_remote(
                    cfg.target_host,
                    cfg.ssh_user,
                    cfg.ssh_port,
                    cfg.mm_systemd_service,
                    cfg.mm_compose_dir,
                    cfg.mm_container
                )
        append_log(log_file, f"MM-RESTART: ok={mm_ok}")

        time.sleep(5)
        if cfg.mode == "ip":
            ok2 = (not cfg.require_ping or check_ping(cfg.target_host)) and (not cfg.require_ssh or check_ssh(cfg.target_host, cfg.ssh_user, cfg.ssh_port))
            if ok2:
                notify.notify("fixed/soft", cfg.target_host, cfg.ssh_user, "recovered after MeshMonitor restart")
                append_log(log_file, "FIXED: recovered after MeshMonitor restart (no reboot)")
                return 0
        else:
            if check_tcp(cfg.ble_bridge_host, cfg.ble_bridge_port, timeout_s=2.5):
                notify.notify("fixed/soft", cfg.target_host, cfg.ssh_user, "bridge recovered after MeshMonitor restart")
                append_log(log_file, "FIXED: bridge recovered after MeshMonitor restart (no reboot)")
                return 0

    # 3) Reboot device (optional)
    if not cfg.allow_reboot:
        notify.notify("action/stop", cfg.target_host, cfg.ssh_user, "reboot disabled; stopping after restarts")
        append_log(log_file, "STOP: reboot disabled; leaving unhealthy")
        return 1

    notify.notify("action/reboot-device", cfg.target_host, cfg.ssh_user, "escalating to device reboot")
    append_log(log_file, "REMEDIATE: rebooting device")
    reboot_ok = reboot_device_remote(cfg.target_host, cfg.ssh_user, cfg.ssh_port)
    append_log(log_file, f"REBOOT: command_sent={reboot_ok}")

    # 4) Wait for recovery and notify
    recovered = False
    if cfg.mode == "ip":
        recovered = wait_for_recovery_ip(cfg.target_host, cfg.ssh_user, cfg.ssh_port, cfg.recovery_timeout, cfg.check_interval)
    else:
        recovered = wait_for_recovery_ble(cfg.ble_bridge_host, cfg.ble_bridge_port, cfg.recovery_timeout, cfg.check_interval)

    if recovered:
        notify.notify("recovered", cfg.target_host, cfg.ssh_user, "target recovered after reboot")
        append_log(log_file, "RECOVERED: target back online")

        if cfg.meshtastic_notify:
            msg = render_text(cfg.meshtastic_text, cfg.target_host)
            sent = meshtastic_send_text(
                log_file=log_file,
                text=msg,
                tcp_host=cfg.meshtastic_tcp_host,
                tcp_port=cfg.meshtastic_tcp_port,
                serial_path=cfg.meshtastic_serial,
                dest=cfg.meshtastic_dest,
            )
            notify.notify("meshtastic-notify", cfg.target_host, cfg.ssh_user, f"sent={sent} text='{msg}'")

        return 0

    notify.notify("recovery-timeout", cfg.target_host, cfg.ssh_user, f"did not recover within {cfg.recovery_timeout}s")
    append_log(log_file, f"TIMEOUT: did not recover within {cfg.recovery_timeout}s")
    return 1


# ----------------------------
# CLI
# ----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meshmonitor-watchandreboot",
        description="All-in-one watchdog + restart bridge/MeshMonitor + reboot + notifications (MeshTools v2.0 public)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch-once", help="Run one watchdog evaluation and remediate if threshold reached (cron entrypoint).")
    w.add_argument("host", nargs="?", default=DEFAULT_TARGET_HOST, help="Target host/IP (or set MM_TARGET_HOST).")
    w.add_argument("--mode", default=DEFAULT_MODE, choices=["ip", "ble"], help="Watch mode (default: ip).")
    w.add_argument("--user", default=DEFAULT_REMOTE_USER, help="SSH user for remote actions.")
    w.add_argument("--port", type=int, default=DEFAULT_SSH_PORT, help="SSH port for remote actions.")

    w.add_argument("--fail-threshold", type=int, default=DEFAULT_FAIL_THRESHOLD)
    w.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_SECONDS)
    w.add_argument("--recovery-timeout", type=int, default=DEFAULT_RECOVERY_TIMEOUT)
    w.add_argument("--check-interval", type=int, default=DEFAULT_CHECK_INTERVAL)

    w.add_argument("--require-ping", action="store_true", default=DEFAULT_REQUIRE_PING)
    w.add_argument("--no-require-ping", action="store_false", dest="require_ping")
    w.add_argument("--require-ssh", action="store_true", default=DEFAULT_REQUIRE_SSH)
    w.add_argument("--no-require-ssh", action="store_false", dest="require_ssh")

    # BLE bridge
    w.add_argument("--ble-bridge-host", default=DEFAULT_BLE_BRIDGE_HOST)
    w.add_argument("--ble-bridge-port", type=int, default=DEFAULT_BLE_BRIDGE_PORT)
    w.add_argument("--ble-bridge-restart-mode", default=DEFAULT_BLE_BRIDGE_RESTART_MODE, choices=["local", "remote", "off"])
    w.add_argument("--ble-bridge-systemd-service", default=DEFAULT_BLE_BRIDGE_SYSTEMD_SERVICE)
    w.add_argument("--ble-bridge-compose-dir", default=DEFAULT_BLE_BRIDGE_COMPOSE_DIR)
    w.add_argument("--ble-bridge-container", default=DEFAULT_BLE_BRIDGE_CONTAINER_NAME)

    # MeshMonitor restart
    w.add_argument("--mm-restart-mode", default=DEFAULT_MM_RESTART_MODE, choices=["local", "remote", "off"])
    w.add_argument("--mm-systemd-service", default=DEFAULT_MM_SYSTEMD_SERVICE)
    w.add_argument("--mm-compose-dir", default=DEFAULT_MM_COMPOSE_DIR)
    w.add_argument("--mm-container", default=DEFAULT_MM_CONTAINER_NAME)

    # Reboot enable/disable
    w.add_argument("--allow-reboot", action="store_true", default=DEFAULT_ALLOW_REBOOT)
    w.add_argument("--no-allow-reboot", action="store_false", dest="allow_reboot")

    # Meshtastic notify (optional)
    w.add_argument("--meshtastic-notify", action="store_true", default=DEFAULT_MESHTASTIC_NOTIFY)
    w.add_argument("--meshtastic-text", default=DEFAULT_MESHTASTIC_TEXT)
    w.add_argument("--meshtastic-tcp-host", default=DEFAULT_MESHTASTIC_TCP_HOST)
    w.add_argument("--meshtastic-tcp-port", type=int, default=DEFAULT_MESHTASTIC_TCP_PORT)
    w.add_argument("--meshtastic-serial", default=DEFAULT_MESHTASTIC_SERIAL)
    w.add_argument("--meshtastic-dest", default=DEFAULT_MESHTASTIC_DEST)

    t = sub.add_parser("status", help="Print detected capabilities + current defaults.")
    t.add_argument("--mode", default=DEFAULT_MODE, choices=["ip", "ble"])
    t.add_argument("--host", default=DEFAULT_TARGET_HOST)

    return p


def cmd_status(args: argparse.Namespace, log_file: Path) -> int:
    caps = {
        "ping": which("ping"),
        "ssh": which("ssh"),
        "systemctl": which("systemctl"),
        "docker": which("docker"),
        "docker-compose": which("docker-compose"),
        "mosquitto_pub": which("mosquitto_pub"),
        "curl": which("curl"),
        "meshtastic_py": False,
    }
    try:
        import meshtastic  # noqa: F401
        caps["meshtastic_py"] = True
    except Exception:
        caps["meshtastic_py"] = False

    info = {
        "mode": args.mode,
        "host": args.host,
        "capabilities": caps,
        "notes": {
            "lineage": "Originally internal MeshTools; v2.0 public as Watch and Reboot; replaces prior scripts.",
            "state_dir": DEFAULT_STATE_DIR,
            "log_file": DEFAULT_LOG_FILE,
        }
    }
    print(json.dumps(info, indent=2))
    append_log(log_file, f"STATUS: {json.dumps(info, separators=(',', ':'))}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    state_dir = Path(DEFAULT_STATE_DIR)
    log_file = ensure_paths(state_dir, Path(DEFAULT_LOG_FILE))

    notifier = Notifier(
        mqtt_host=DEFAULT_MQTT_HOST,
        mqtt_port=DEFAULT_MQTT_PORT,
        mqtt_topic_prefix=DEFAULT_MQTT_TOPIC_PREFIX,
        webhook_url=DEFAULT_WEBHOOK_URL,
        notify_cmd=DEFAULT_NOTIFY_CMD,
        log_file=log_file,
    )

    if args.cmd == "status":
        return cmd_status(args, log_file)

    if args.cmd == "watch-once":
        if not args.host:
            append_log(log_file, "ERROR: target host not provided (arg or MM_TARGET_HOST)")
            print("ERROR: target host required (provide as argument or set MM_TARGET_HOST)", file=sys.stderr)
            return 2

        cfg = WatchConfig(
            mode=args.mode,
            target_host=args.host,
            ssh_user=args.user,
            ssh_port=args.port,

            fail_threshold=args.fail_threshold,
            cooldown_seconds=args.cooldown,
            recovery_timeout=args.recovery_timeout,
            check_interval=args.check_interval,

            require_ping=args.require_ping,
            require_ssh=args.require_ssh,

            ble_bridge_host=args.ble_bridge_host,
            ble_bridge_port=args.ble_bridge_port,
            ble_bridge_restart_mode=args.ble_bridge_restart_mode,
            ble_bridge_systemd_service=args.ble_bridge_systemd_service,
            ble_bridge_compose_dir=args.ble_bridge_compose_dir,
            ble_bridge_container=args.ble_bridge_container,

            mm_restart_mode=args.mm_restart_mode,
            mm_systemd_service=args.mm_systemd_service,
            mm_compose_dir=args.mm_compose_dir,
            mm_container=args.mm_container,

            allow_reboot=args.allow_reboot,

            meshtastic_notify=args.meshtastic_notify,
            meshtastic_text=args.meshtastic_text,
            meshtastic_tcp_host=args.meshtastic_tcp_host,
            meshtastic_tcp_port=args.meshtastic_tcp_port,
            meshtastic_serial=args.meshtastic_serial,
            meshtastic_dest=args.meshtastic_dest,
        )

        return watch_once(cfg, state_dir, log_file, notifier)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
