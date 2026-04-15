#!/usr/bin/env python3
"""
Linux heartbeat agent for SE9 OPS.

Usage:
python3 linux_heartbeat_agent.py \
  --server https://your-ops-server \
  --token <heartbeat_token> \
  --interval 30
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AGENT_VERSION = "1.0.2"
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_ULA = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SE9 OPS Linux heartbeat agent")
    parser.add_argument("--server", required=True, help="Server URL, e.g. https://ops.example.com")
    parser.add_argument("--token", required=True, help="Heartbeat token")
    parser.add_argument("--interval", type=int, default=30, help="Heartbeat interval seconds")
    parser.add_argument("--timeout", type=int, default=8, help="HTTP timeout seconds")
    return parser.parse_args()


def read_cpu_times() -> tuple[int, int]:
    with open("/proc/stat", "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    parts = first_line.split()
    values = [int(x) for x in parts[1:]]
    idle = values[3] + values[4] if len(values) > 4 else values[3]
    total = sum(values)
    return idle, total


def calc_cpu_percent(prev: tuple[int, int], curr: tuple[int, int]) -> float | None:
    prev_idle, prev_total = prev
    curr_idle, curr_total = curr
    total_delta = curr_total - prev_total
    idle_delta = curr_idle - prev_idle
    if total_delta <= 0:
        return None
    usage = 100.0 * (1.0 - (idle_delta / total_delta))
    return max(0.0, min(100.0, usage))


def read_mem_percent() -> float | None:
    mem_total = None
    mem_available = None
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_available = int(line.split()[1])
            if mem_total is not None and mem_available is not None:
                break
    if not mem_total or mem_available is None:
        return None
    used = mem_total - mem_available
    return max(0.0, min(100.0, (used / mem_total) * 100.0))


def read_disk_percent(path: str = "/") -> float | None:
    stat = os.statvfs(path)
    if stat.f_blocks <= 0:
        return None
    used_blocks = stat.f_blocks - stat.f_bfree
    return max(0.0, min(100.0, (used_blocks / stat.f_blocks) * 100.0))


def read_load_1m() -> float | None:
    try:
        return float(os.getloadavg()[0])
    except Exception:
        return None


def read_uptime_seconds() -> int | None:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            uptime_text = f.readline().split()[0]
        return int(float(uptime_text))
    except Exception:
        return None


def read_tailscale_ipv4() -> str | None:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv4Address) and ip in TAILSCALE_CGNAT:
            return candidate
    return None


def build_payload(prev_cpu: tuple[int, int], curr_cpu: tuple[int, int]) -> dict[str, Any]:
    return {
        "cpu_percent": calc_cpu_percent(prev_cpu, curr_cpu),
        "memory_percent": read_mem_percent(),
        "disk_percent": read_disk_percent("/"),
        "load_1m": read_load_1m(),
        "uptime_seconds": read_uptime_seconds(),
        "agent_version": AGENT_VERSION,
        "tailscale_ipv4": read_tailscale_ipv4(),
    }


def should_bypass_proxy(server_url: str) -> bool:
    host = urllib.parse.urlparse(server_url).hostname or ""
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip in TAILSCALE_CGNAT
        or ip in TAILSCALE_ULA
    )


def open_url(request: urllib.request.Request, timeout_sec: int, bypass_proxy: bool):
    if bypass_proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout_sec)
    return urllib.request.urlopen(request, timeout=timeout_sec)


def post_heartbeat(
    url: str,
    payload: dict[str, Any],
    timeout_sec: int,
    *,
    bypass_proxy: bool,
) -> tuple[bool, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with open_url(req, timeout_sec, bypass_proxy) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        return True, body
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", errors="ignore")
        return False, f"HTTP {exc.code}: {msg}"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    args = parse_args()
    if args.interval < 5:
        print("interval 太短，建议不小于 5 秒")
        return 1

    server = args.server.rstrip("/")
    heartbeat_url = f"{server}/api/heartbeat/{args.token}"
    bypass_proxy = should_bypass_proxy(server)

    print("SE9 OPS heartbeat agent started")
    print(f"host={socket.gethostname()} interval={args.interval}s url={heartbeat_url}")
    if bypass_proxy:
        print("proxy bypass enabled for private/local server")

    prev_cpu = read_cpu_times()
    time.sleep(0.2)

    while True:
        curr_cpu = read_cpu_times()
        payload = build_payload(prev_cpu, curr_cpu)
        prev_cpu = curr_cpu

        ok, message = post_heartbeat(
            heartbeat_url,
            payload,
            args.timeout,
            bypass_proxy=bypass_proxy,
        )
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            print(f"[{timestamp}] heartbeat ok")
        else:
            print(f"[{timestamp}] heartbeat failed: {message}")

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(0)
