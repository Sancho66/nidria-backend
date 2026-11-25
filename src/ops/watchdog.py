"""Observe the web machine from the dedicated worker. Persist attempts before recovery."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any
from urllib import request

from src.core.config import get_settings
from src.ops.supervise_api import PRIVATE_PORT, read_json

PUBLIC = "https://api.nidria.com/health"
PRIVATE = "app.fly_process_group.kv._metadata.nidria-api.internal"
INTERVAL = 20
COOLDOWN = 1800
State = dict[str, Any]


def decision(state: State, sample: State, public: bool, now: float) -> tuple[list[str], str | None]:
    fresh = bool(sample.get("generation") and 0 <= now - sample.get("sample_at", 0) < 60)
    healthy = fresh and sample.get("live") is True and sample.get("ready") is True and public
    state["healthy"] = state.get("healthy", 0) + 1 if healthy else 0
    state["failures"] = 0 if healthy else state.get("failures", 0) + 1
    events: list[str] = []
    if healthy:
        state["armed_generation"] = sample["generation"]
        state["last_healthy"] = now
    if fresh:
        state["last_sample"] = sample
    critical = fresh and not public and sample.get("live") is False
    if not critical or state.get("generation") != sample.get("generation"):
        state["live_failures"] = 0
    if critical and state.get("last_probe") != sample["sample_at"]:
        state["live_failures"] = state.get("live_failures", 0) + 1
    state["generation"] = sample.get("generation")
    state["last_probe"] = sample.get("sample_at")
    if state.get("incident") and state["healthy"] >= 3:
        events.append("retabli")
        state["incident"] = None
        state["attempted"] = False
    if state["failures"] >= 3 and not state.get("incident"):
        state["incident"] = uuid.uuid4().hex
        state["attempted"] = False
        events.append("indisponible")
    if (
        state.get("incident")
        and state.get("armed_generation") == sample.get("generation")
        and not state.get("attempted")
        and state.get("live_failures", 0) >= 3
        and sample.get("live_failures", 0) >= 3
        and now - state.get("last_restart", -COOLDOWN) >= COOLDOWN
    ):
        state.update(attempted=True, last_restart=now)
        events.append("redemarrage")
        return events, str(sample["generation"])
    return events, None


class Journal:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS monitor (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, at REAL, value TEXT);
            CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, body TEXT);
        """)

    def observe(self, sample: State, public: bool, now: float) -> str | None:
        with self.db:
            row = self.db.execute("SELECT value FROM monitor WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else {}
            old_incident = state.get("incident")
            events, generation = decision(state, sample, public, now)
            self.db.execute("INSERT OR REPLACE INTO monitor VALUES(1,?)", (json.dumps(state),))
            evidence = {"time": now, "public_ready": public, "private": sample}
            self.db.execute(
                "INSERT INTO samples(at,value) VALUES(?,?)", (now, json.dumps(evidence))
            )
            self.db.execute("DELETE FROM samples WHERE at < ?", (now - 86400,))
            for event in events:
                identifier = f"{state.get('incident') or old_incident}-{event}"
                details = {
                    "etat": event,
                    **evidence,
                    "dernier_releve": state.get("last_sample"),
                    "dernier_succes": state.get("last_healthy"),
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO alerts VALUES(?,?)", (identifier, json.dumps(details))
                )
                print("watchdog incident", identifier, json.dumps(details), flush=True)
        return generation

    def deliver(self) -> None:
        row = self.db.execute("SELECT id,body FROM alerts ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return
        settings = get_settings()
        if (
            not settings.resend_api_key
            or not settings.signup_alert_recipients
            or (settings.mock_email if settings.mock_email is not None else settings.mock_services)
        ):
            raise RuntimeError("Operations mail channel unavailable")
        body = json.loads(row[1])
        titles = {
            "indisponible": "indisponibilité détectée",
            "redemarrage": "tentative de redémarrage",
            "retabli": "service rétabli",
        }
        message = {
            "from": settings.email_from,
            "to": settings.signup_alert_recipients,
            "subject": f"Nidria API · {titles[body['etat']]}",
            "text": json.dumps(body, indent=2),
        }
        call = request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(message).encode(),
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": f"nidria-api-{row[0]}",
            },
            method="POST",
        )
        with request.urlopen(call, timeout=5) as response:  # noqa: S310
            if not json.loads(response.read(2048)).get("id"):
                raise RuntimeError("Unconfirmed alert")
        with self.db:
            self.db.execute("DELETE FROM alerts WHERE id=?", (row[0],))


def private_call(key: str, generation: str | None = None) -> State:
    headers = {"Authorization": f"Bearer {key}"}
    if generation is not None:
        headers["X-Generation"] = generation
    connection = HTTPConnection(PRIVATE, PRIVATE_PORT, timeout=6)
    try:
        connection.request(
            "POST" if generation else "GET",
            "/recover" if generation else "/status",
            headers=headers,
        )
        response = connection.getresponse()
        if response.status != (202 if generation else 200):
            raise OSError(f"Private supervisor returned {response.status}")
        return {} if generation else dict(json.loads(response.read(8192)))
    finally:
        connection.close()


def main() -> int:
    if (
        os.environ.get("FLY_APP_NAME") != "nidria-api"
        or os.environ.get("FLY_PROCESS_GROUP") != "worker"
    ):
        raise RuntimeError("The watchdog runs only on the production worker")
    key = os.environ["API_WATCHDOG_KEY"]
    if len(key) < 32 or not os.path.ismount("/watchdog"):
        raise RuntimeError("Watchdog requires its persistent journal and private key")
    journal = Journal(Path("/watchdog/monitor.db"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    command = [sys.executable, "-m", "src.email_campaigns.sync_worker"]
    child = subprocess.Popen(command)
    print("watchdog started on dedicated worker", flush=True)
    started = time.monotonic()
    next_launch = started + 60
    try:
        while not stop.is_set():
            if child.poll() is not None and time.monotonic() >= next_launch:
                print("watchdog: restarting exited email_sync worker", flush=True)
                child = subprocess.Popen(command)
                next_launch = time.monotonic() + 60
            try:
                sample = private_call(key)
            except (OSError, ValueError, HTTPException):
                sample = {}
            try:
                public = read_json(PUBLIC) == {"status": "ok", "db": "ok"}
            except (OSError, ValueError, HTTPException):
                public = False
            # A rolling release may still be running migrations before the first sample.
            if sample or time.monotonic() - started >= 180:
                generation = journal.observe(sample, public, time.time())
                if generation:
                    try:
                        private_call(key, generation)
                    except (OSError, ValueError, HTTPException) as exc:
                        print("watchdog recovery unconfirmed:", type(exc).__name__, flush=True)
                try:
                    journal.deliver()
                except (OSError, ValueError, HTTPException, RuntimeError) as exc:
                    print("watchdog alert pending:", type(exc).__name__, flush=True)
            stop.wait(INTERVAL)
    finally:
        journal.db.close()
        child.terminate()
        try:
            child.wait(timeout=9)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
