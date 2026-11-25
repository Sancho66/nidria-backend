import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ops import supervise_api, watchdog
from src.ops.watchdog import Journal, decision


def sample(at: int, live: bool, ready: bool, generation: str = "web1") -> dict:
    return {
        "sample_at": at,
        "generation": generation,
        "live": live,
        "ready": ready,
        "live_failures": 0 if live else 3,
        "postgres_established": 4,
        "cpu_percent": 17,
    }


def test_at_most_one_recovery_across_monitor_restart_and_three_healthy_samples(tmp_path: Path):
    path = tmp_path / "monitor.db"
    journal = Journal(path)
    assert journal.observe(sample(1000, True, True), True, 1000) is None
    for at in (1020, 1040):
        assert journal.observe(sample(at, False, False), False, at) is None
    assert journal.observe(sample(1060, False, False), False, 1060) == "web1"
    journal.db.close()
    journal = Journal(path)
    for at in (1080, 1100, 1120):
        assert journal.observe(sample(at, False, False), False, at) is None
    for at in (1140, 1160, 1180):
        journal.observe(sample(at, True, True, "web2"), True, at)
    events = [
        json.loads(row[0]) for row in journal.db.execute("SELECT body FROM alerts ORDER BY rowid")
    ]
    assert [event["etat"] for event in events] == ["indisponible", "redemarrage", "retabli"]
    assert events[0]["dernier_releve"]["postgres_established"] == 4
    assert journal.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 10
    for at in (1200, 1220, 1240):
        assert journal.observe(sample(at, False, False, "web2"), False, at) is None
    journal.db.close()


@pytest.mark.parametrize("failure", ["db", "network", "old", "new_process", "edge"])
def test_database_network_deployment_and_stale_observations_never_restart(failure: str):
    state: dict = {}
    decision(state, sample(1000, True, True), True, 1000)
    events = []
    for at in (1020, 1040, 1060, 1080, 1100):
        observation = {
            "db": sample(at, True, False),
            "network": {},
            "old": sample(990, False, False),
            "new_process": sample(at, False, False, "new"),
            "edge": sample(at, True, True),
        }[failure]
        notices, restart = decision(state, observation, False, at)
        events.extend(notices)
        assert restart is None
    assert events == ["indisponible"]


def test_web_rechecks_liveness_and_rejects_replays(monkeypatch: pytest.MonkeyPatch):
    supervisor = supervise_api.WebSupervisor(5432)
    supervisor.generation = "new"
    supervisor.snapshot = sample(1000, False, False, "new")
    monkeypatch.setattr(supervise_api.time, "time", lambda: 1003)
    monkeypatch.setattr(supervise_api, "probe", lambda *_: True)
    assert not supervisor.recover("new")
    monkeypatch.setattr(supervise_api, "probe", lambda *_: False)
    assert not supervisor.recover("old")
    assert supervisor.recover("new")
    assert not supervisor.recover("new")


def test_alert_survives_an_unconfirmed_delivery_and_reuses_its_id(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "alerts.db")
    for at in (1000, 1020, 1040):
        journal.observe(sample(at, True, False), False, at)
    settings = SimpleNamespace(
        resend_api_key="fixture",
        signup_alert_recipients=["ops@example.test"],
        mock_email=False,
        email_from="service@example.test",
    )
    monkeypatch.setattr(watchdog, "get_settings", lambda: settings)
    keys = []

    def sink(call, timeout):
        assert json.loads(call.data)["to"] == ["ops@example.test"]
        keys.append(call.get_header("Idempotency-key"))
        if len(keys) == 1:
            raise TimeoutError
        return BytesIO(b'{"id":"fixture-confirmation"}')

    monkeypatch.setattr(watchdog.request, "urlopen", sink)
    with pytest.raises(TimeoutError):
        journal.deliver()
    journal.deliver()
    journal.deliver()
    assert len(keys) == 2 and keys[0] and keys[0] == keys[1]
    assert journal.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    journal.db.close()


def test_accepted_recovery_replaces_only_the_api_child(monkeypatch):
    supervisor = supervise_api.WebSupervisor(5432)
    children = []
    # Deterministic probes; the decision and the supervisor's process loop are real.
    monkeypatch.setattr(supervisor, "sample", lambda: None)
    monkeypatch.setattr(supervise_api, "probe", lambda *_: False)

    class Child:
        returncode = None

        def __init__(self, command):
            assert command[1:4] == ["-m", "uvicorn", "src.main:app"]
            children.append(self)
            self.generation = supervisor.generation
            self.terminated = 0
            if len(children) == 1:
                supervisor.snapshot = {"live_failures": 3, "sample_at": supervise_api.time.time()}
                assert supervisor.recover(self.generation)
            else:
                assert len(children) == 2 and self.generation != children[0].generation
                assert not supervisor.recover(children[0].generation)
                supervisor.stop.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated += 1
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(supervise_api.subprocess, "Popen", Child)
    assert supervisor.run() == 0
    assert [c.terminated for c in children] == [1, 1]
