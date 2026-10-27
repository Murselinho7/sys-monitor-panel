#!/usr/bin/env python3
"""Local system monitor. Standard library only.

Serves a single-page dashboard and two live feeds:

- GET /api/stats   one JSON sample
- GET /api/events  Server-Sent Events, one sample per second

Metrics come from /proc and os.statvfs on Linux, and from PowerShell CIM
(with a WMIC fallback) on Windows. A background sampler keeps the HTTP
handlers from paying that cost on every request.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CREATE_NO_WINDOW = 0x08000000
ROOT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ROOT, "index.html")
SCRIPT = os.path.join(ROOT, "dashboard.js")

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$osInfo = Get-CimInstance Win32_OperatingSystem
$drive = $env:SystemDrive
if (-not $drive) { $drive = 'C:' }
$disk = Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='" + $drive + "'")
if (-not $disk) { throw "No logical disk for $drive" }
$cpuValue = 0.0
if ($null -ne $cpu) { $cpuValue = [math]::Round([double]$cpu, 1) }
$total = [int64]$osInfo.TotalVisibleMemorySize * 1024
$free = [int64]$osInfo.FreePhysicalMemory * 1024
$used = $total - $free
if ($used -lt 0) { $used = 0 }
$diskTotal = [int64]$disk.Size
$diskFree = [int64]$disk.FreeSpace
$diskUsed = $diskTotal - $diskFree
if ($diskUsed -lt 0) { $diskUsed = 0 }
$uptime = [int]((Get-Date) - $osInfo.LastBootUpTime).TotalSeconds
[pscustomobject]@{
  cpu_percent = $cpuValue
  mem_total = $total
  mem_used = $used
  disk_total = $diskTotal
  disk_used = $diskUsed
  disk_free = $diskFree
  disk_mount = [string]$drive
  uptime_seconds = $uptime
  process_count = [int]$osInfo.NumberOfProcesses
} | ConvertTo-Json -Compress
"""


class CpuDelta(object):
    """CPU percent from two /proc/stat samples."""

    def __init__(self):
        self.total = None
        self.idle = None

    def update(self, total, idle):
        if self.total is None or total < self.total:
            self.total = total
            self.idle = idle
            return 0.0
        delta_total = total - self.total
        delta_idle = idle - self.idle
        self.total = total
        self.idle = idle
        if delta_total <= 0:
            return 0.0
        used = (1.0 - (delta_idle / float(delta_total))) * 100.0
        return round(max(0.0, min(100.0, used)), 1)


_CPU = CpuDelta()


def percent(used, total):
    if not total:
        return 0.0
    return round(max(0.0, min(100.0, (float(used) / float(total)) * 100.0)), 1)


def parse_cpu_line(line):
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        raise ValueError("missing aggregate cpu line")
    numbers = [int(part) for part in parts[1:]]
    idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
    return sum(numbers), idle


def parse_meminfo(text):
    values = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        bits = rest.split()
        if not bits:
            continue
        values[key] = int(bits[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return total, used


def _run(args, timeout):
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    completed = subprocess.run(args, **kwargs)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise RuntimeError(detail.splitlines()[0][:300] if detail else "command failed")
    return completed.stdout


def _sample_linux():
    with open("/proc/stat", "r", encoding="utf-8") as handle:
        total, idle = parse_cpu_line(handle.readline())
    cpu = _CPU.update(total, idle)
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        mem_total, mem_used = parse_meminfo(handle.read())
    with open("/proc/uptime", "r", encoding="utf-8") as handle:
        uptime = int(float(handle.read().split()[0]))
    with open("/proc/loadavg", "r", encoding="utf-8") as handle:
        load_bits = handle.read().split()
    load = [float(load_bits[0]), float(load_bits[1]), float(load_bits[2])]
    processes = sum(1 for name in os.listdir("/proc") if name.isdigit())
    disk_total, disk_used, disk_free = _disk_unix("/")
    return _metrics(
        cpu_percent=cpu,
        mem_total=mem_total,
        mem_used=mem_used,
        disk_total=disk_total,
        disk_used=disk_used,
        disk_free=disk_free,
        disk_mount="/",
        uptime_seconds=uptime,
        process_count=processes,
        load_avg=load,
    )


def _disk_unix(path):
    stats = os.statvfs(path)
    total = stats.f_frsize * stats.f_blocks
    free = stats.f_frsize * stats.f_bavail
    used = max(0, total - free)
    return total, used, free


def _sample_windows():
    try:
        raw_text = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SCRIPT],
            timeout=25,
        )
        raw = json.loads(raw_text.strip().lstrip("\ufeff"))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError):
        raw = _sample_windows_wmic()
    return _metrics(
        cpu_percent=float(raw["cpu_percent"]),
        mem_total=int(raw["mem_total"]),
        mem_used=int(raw["mem_used"]),
        disk_total=int(raw["disk_total"]),
        disk_used=int(raw["disk_used"]),
        disk_free=int(raw["disk_free"]),
        disk_mount=str(raw["disk_mount"]),
        uptime_seconds=int(raw["uptime_seconds"]),
        process_count=int(raw["process_count"]),
        load_avg=None,
    )


def _sample_windows_wmic():
    drive = os.environ.get("SystemDrive", "C:")
    cpu_text = _run(["wmic", "cpu", "get", "loadpercentage", "/value"], timeout=20)
    mem_text = _run(
        ["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize,NumberOfProcesses,LastBootUpTime", "/value"],
        timeout=20,
    )
    disk_text = _run(
        ["wmic", "logicaldisk", "where", "DeviceID='%s'" % drive, "get", "Size,FreeSpace", "/value"],
        timeout=20,
    )
    cpu = _wmic_map(cpu_text)
    mem = _wmic_map(mem_text)
    disk = _wmic_map(disk_text)
    total = int(mem["TotalVisibleMemorySize"]) * 1024
    free = int(mem["FreePhysicalMemory"]) * 1024
    used = max(0, total - free)
    disk_total = int(disk["Size"])
    disk_free = int(disk["FreeSpace"])
    boot = mem["LastBootUpTime"]
    uptime = _wmic_uptime(boot)
    return {
        "cpu_percent": float(cpu.get("LoadPercentage", 0)),
        "mem_total": total,
        "mem_used": used,
        "disk_total": disk_total,
        "disk_used": max(0, disk_total - disk_free),
        "disk_free": disk_free,
        "disk_mount": drive,
        "uptime_seconds": uptime,
        "process_count": int(mem.get("NumberOfProcesses", 0)),
    }


def _wmic_map(text):
    found = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip()
    return found


def _wmic_uptime(stamp):
    # WMIC LastBootUpTime looks like 20261027xxxxxx.mmm+offset
    digits = "".join(ch for ch in stamp if ch.isdigit())
    if len(digits) < 14:
        return 0
    boot = time.strptime(digits[:14], "%Y%m%d%H%M%S")
    return max(0, int(time.time() - time.mktime(boot)))


def _metrics(cpu_percent, mem_total, mem_used, disk_total, disk_used, disk_free, disk_mount, uptime_seconds, process_count, load_avg):
    mem_free = max(0, mem_total - mem_used)
    return {
        "cpu_percent": round(max(0.0, min(100.0, float(cpu_percent))), 1),
        "cpu_count": os.cpu_count() or 0,
        "memory": {
            "total": mem_total,
            "used": mem_used,
            "free": mem_free,
            "percent": percent(mem_used, mem_total),
        },
        "disk": {
            "total": disk_total,
            "used": disk_used,
            "free": disk_free,
            "percent": percent(disk_used, disk_total),
            "mount": disk_mount,
        },
        "uptime_seconds": max(0, int(uptime_seconds)),
        "process_count": int(process_count),
        "load_avg": load_avg,
    }


def collect():
    system = platform.system().lower()
    if system == "windows":
        sample = _sample_windows()
    elif system == "linux":
        sample = _sample_linux()
    else:
        raise RuntimeError("unsupported system: %s" % platform.system())
    sample["hostname"] = platform.node()
    sample["system"] = platform.system()
    sample["platform"] = platform.platform()
    sample["collected_at"] = time.time()
    sample["error"] = None
    return sample


class Sampler(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="sys-monitor-sampler", daemon=True)

    def start(self):
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with self._lock:
                if self._snapshot:
                    return
            time.sleep(0.05)

    def latest(self):
        with self._lock:
            if not self._snapshot:
                return {
                    "status": "starting",
                    "error": None,
                    "hostname": platform.node(),
                    "system": platform.system(),
                }
            return self._snapshot

    def close(self):
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snapshot = collect()
            except Exception as exc:
                with self._lock:
                    previous = dict(self._snapshot)
                previous["error"] = str(exc)
                previous["collected_at"] = time.time()
                snapshot = previous
            with self._lock:
                self._snapshot = snapshot
            remaining = 1.0 - (time.monotonic() - started)
            if self._stop.wait(max(0.05, remaining)):
                return


SAMPLER = Sampler()


class Handler(BaseHTTPRequestHandler):
    server_version = "SysMonitor/1.0"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_file(PAGE, "text/html; charset=utf-8")
        elif path == "/dashboard.js":
            self._send_file(SCRIPT, "text/javascript; charset=utf-8")
        elif path == "/api/stats":
            self._send_json(SAMPLER.latest())
        elif path == "/api/events":
            self._send_events()
        else:
            self._send_json({"error": "not found"}, status=404)

    def _send_file(self, filepath, content_type):
        try:
            with open(filepath, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json({"error": "dashboard file is missing"}, status=500)
            return
        self._send_bytes(body, content_type)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status=status)

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_events(self):
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                message = b"data: " + json.dumps(SAMPLER.latest()).encode("utf-8") + b"\n\n"
                self.wfile.write(("%x\r\n" % len(message)).encode("ascii"))
                self.wfile.write(message + b"\r\n")
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def log_message(self, fmt, *args):
        if self.path.split("?", 1)[0] == "/api/events":
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live local CPU, memory, and disk dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    args = parser.parse_args(argv)
    if not os.path.isfile(PAGE) or not os.path.isfile(SCRIPT):
        sys.stderr.write("index.html or dashboard.js is missing next to server.py\n")
        return 1
    print("Collecting the first sample...", flush=True)
    SAMPLER.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("Sys monitor listening on %s" % url, flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
        SAMPLER.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
