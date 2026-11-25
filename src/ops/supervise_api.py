"""Sample the web VM outside Uvicorn. Only the private Fly network can reach this port."""

import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit

PROBE_INTERVAL = 15
PRIVATE_PORT = 9091


def read_json(url: str, timeout: float = 5) -> Any:
    with request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read(8192))


def probe(path: str, expected: Any) -> bool:
    try:
        return bool(read_json(f"http://127.0.0.1:8000/{path}", 4) == expected)
    except (OSError, ValueError, error.URLError, HTTPException):
        return False


def resource_sample(pg_port: int) -> dict[str, int]:
    fields = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0])
    connections = 0
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        for row in table.read_text().splitlines()[1:]:
            _, _, remote, state, *_ = row.split()
            if state == "01" and int(remote.split(":")[1], 16) == pg_port:
                connections += 1
    return {
        "cpu_total": sum(fields),
        "cpu_idle": sum(fields[3:5]),
        "memory_total_kib": memory["MemTotal"],
        "memory_available_kib": memory["MemAvailable"],
        "postgres_established": connections,
    }


class WebSupervisor:
    def __init__(self, pg_port: int) -> None:
        self.pg_port = pg_port
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.restart = threading.Event()
        self.generation = ""
        self.snapshot: dict[str, Any] = {}

    def sample(self) -> None:
        previous: dict[str, int] = {}
        while not self.stop.is_set():
            generation = self.generation
            live = probe("ping", "pong")
            ready = probe("health", {"status": "ok", "db": "ok"})
            cpu = None
            try:
                metrics = resource_sample(self.pg_port)
                total = metrics["cpu_total"] - previous.get("cpu_total", metrics["cpu_total"])
                idle = metrics["cpu_idle"] - previous.get("cpu_idle", metrics["cpu_idle"])
                cpu = round(100 * (1 - idle / total), 1) if total > 0 else None
                previous = metrics
            except (OSError, KeyError, ValueError, IndexError):
                metrics = {}
            with self.lock:
                if generation == self.generation:
                    fails = 0 if live else self.snapshot.get("live_failures", 0) + 1
                    self.snapshot = {
                        "generation": generation,
                        "sample_at": time.time(),
                        "live": live,
                        "ready": ready,
                        "live_failures": fails,
                        "cpu_percent": cpu,
                        **metrics,
                    }
            self.stop.wait(PROBE_INTERVAL)

    def recover(self, generation: str) -> bool:
        with self.lock:
            snap = self.snapshot
            if (
                not generation
                or self.generation != generation
                or self.restart.is_set()
                or snap.get("live_failures", 0) < 3
                or time.time() - snap["sample_at"] > 45
            ):
                return False
            if probe("ping", "pong"):
                return False
            self.restart.set()
            return True

    def run(self) -> int:
        threading.Thread(target=self.sample, daemon=True).start()
        while not self.stop.is_set():
            with self.lock:
                self.generation = uuid.uuid4().hex
                self.snapshot = {}
                self.restart.clear()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "src.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                ]
            )
            while child.poll() is None and not self.restart.is_set() and not self.stop.wait(0.25):
                pass
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if not self.restart.is_set() or self.stop.is_set():
                return 0 if self.stop.is_set() else child.returncode or 1
            print("watchdog: restarting the unresponsive API process", flush=True)
        return 0


class _PrivateServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def main() -> int:
    key = os.environ["API_WATCHDOG_KEY"]
    if len(key) < 32 or os.environ.get("FLY_PROCESS_GROUP") != "app":
        raise RuntimeError("Private API supervisor configuration is incomplete")
    supervisor = WebSupervisor(urlsplit(os.environ["DATABASE_URL"]).port or 5432)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def authorized(self) -> bool:
            return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {key}")

        def do_GET(self) -> None:
            if not self.authorized() or self.path != "/status":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with supervisor.lock:
                body = json.dumps(supervisor.snapshot).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self.authorized() or self.path != "/recover":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            accepted = supervisor.recover(self.headers.get("X-Generation", ""))
            self.send_response(HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT)
            self.end_headers()

    with _PrivateServer((os.environ["FLY_PRIVATE_IP"], PRIVATE_PORT), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: supervisor.stop.set())
        signal.signal(signal.SIGINT, lambda *_: supervisor.stop.set())
        return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
