from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from vast_broker import supervisor as worker_module
from vast_broker.journal import LeaseJournal
from vast_broker.process_supervisor import ProcessLeaseSupervisor


def test_separate_supervisor_process_survives_controller_return_and_cleans_at_deadline(tmp_path, monkeypatch):
    state_file = tmp_path / "fake-provider.json"
    state_file.write_text(json.dumps({"instances": [{"id": "owned", "label": "owned", "status": "running"}]}), encoding="utf-8")
    monkeypatch.setenv("VBR_FAKE_PROVIDER_STATE", str(state_file))
    monkeypatch.setenv("VAST_BROKER_PROVIDER_FACTORY", "lease_worker_fakes:make_provider")
    package_root = str(Path(__file__).resolve().parents[1] / "src")
    test_root = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([package_root, test_root, os.environ.get("PYTHONPATH", "")]))
    now = time.time()
    journal = LeaseJournal(tmp_path / "journal")
    journal.save({"request_id": "independent", "state": "READY", "created_epoch": now - 10,
                  "hard_deadline_epoch": now + 0.4, "ready_epoch": now - 10,
                  "last_inference_epoch": now - 10, "policy": {}, "owned_instance_ids": ["owned"], "events": []})
    worker = ProcessLeaseSupervisor(journal.directory, poll_seconds=0.05, startup_timeout_seconds=5)
    worker.start("independent")
    # The launcher has no ownership over the child after startup; no controller call is needed.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not state["instances"]:
            break
        time.sleep(0.05)
    assert json.loads(state_file.read_text(encoding="utf-8"))["instances"] == []
    while time.monotonic() < deadline and journal.load("independent")["state"] != "DESTROYED":
        time.sleep(0.05)
    assert journal.load("independent")["state"] == "DESTROYED"
    while time.monotonic() < deadline and journal.load("independent").get("supervisor_child_status") != "stopped_terminal":
        time.sleep(0.05)
    record = journal.load("independent")
    assert record["supervisor_attempt_count"] >= 1
    assert record["supervisor_last_attempt_state"] == "succeeded"
    assert isinstance(record["supervisor_heartbeat_epoch"], (int, float))
    assert record["supervisor_child_status"] == "stopped_terminal"


def test_worker_persists_sanitized_tick_error_and_recovers(tmp_path, monkeypatch):
    journal_dir = tmp_path / "journal"
    journal = LeaseJournal(journal_dir)
    journal.save({"request_id": "tick-retry", "state": "READY", "events": []})
    monkeypatch.setenv("VAST_BROKER_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("VAST_BROKER_PROVIDER_FACTORY", "lease_worker_fakes:make_provider")
    calls = []
    observed = []

    class FailingOnce:
        def __init__(self, provider, journal):
            pass

        def tick(self, request_id):
            calls.append(request_id)
            if len(calls) == 1:
                raise RuntimeError("VAST_API_KEY=credential-must-not-be-saved")
            return {"request_id": request_id, "state": "DESTROYED"}

    def observe_sleep(_):
        observed.append(journal.load("tick-retry"))

    monkeypatch.setattr(worker_module, "LeaseSupervisor", FailingOnce)
    monkeypatch.setattr(worker_module.time, "sleep", observe_sleep)
    ready_file = tmp_path / "ready"
    worker_module.serve("tick-retry", poll_seconds=0.01, ready_file=str(ready_file))

    record = journal.load("tick-retry")
    serialized = json.dumps(record)
    assert calls == ["tick-retry", "tick-retry"]
    assert observed[0]["supervisor_attempt_count"] == 1
    assert observed[0]["supervisor_last_attempt_state"] == "failed"
    assert isinstance(observed[0]["supervisor_heartbeat_epoch"], (int, float))
    assert ready_file.read_text(encoding="utf-8") == "ready"
    assert record["supervisor_attempt_count"] == 2
    assert record["supervisor_last_attempt_state"] == "succeeded"
    assert isinstance(record["supervisor_heartbeat_epoch"], (int, float))
    assert record["supervisor_last_error"]["code"] == "tick_failed"
    assert record["supervisor_last_error"]["exception_type"] == "RuntimeError"
    assert "credential-must-not-be-saved" not in serialized
    assert "VAST_API_KEY" not in serialized


def test_worker_stops_after_failed_clean_terminal_state(tmp_path, monkeypatch):
    journal_dir = tmp_path / "journal"
    journal = LeaseJournal(journal_dir)
    journal.save({"request_id": "terminal", "state": "FAILED_CLEAN", "events": []})
    monkeypatch.setenv("VAST_BROKER_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("VAST_BROKER_PROVIDER_FACTORY", "lease_worker_fakes:make_provider")
    calls = []

    class TerminalTick:
        def __init__(self, provider, journal):
            pass

        def tick(self, request_id):
            calls.append(request_id)
            return {"request_id": request_id, "state": "FAILED_CLEAN"}

    monkeypatch.setattr(worker_module, "LeaseSupervisor", TerminalTick)
    monkeypatch.setattr(worker_module.time, "sleep", lambda _: None)
    worker_module.serve("terminal", poll_seconds=0.01)

    record = journal.load("terminal")
    assert calls == ["terminal"]
    assert record["state"] == "FAILED_CLEAN"
    assert record["supervisor_attempt_count"] == 1
    assert record["supervisor_last_attempt_state"] == "succeeded"


def test_unexpected_child_exit_restarts_until_lease_is_terminal(tmp_path):
    journal = LeaseJournal(tmp_path / "journal")
    journal.save({"request_id": "restart-child", "state": "READY", "events": []})
    children = []

    class FakeChild:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

    def fake_popen(command, **kwargs):
        child = FakeChild()
        children.append(child)
        ready_path = Path(command[command.index("--ready-file") + 1])
        ready_path.write_text("ready", encoding="utf-8")
        return child

    worker = ProcessLeaseSupervisor(
        journal.directory,
        provider_factory="fake:make_provider",
        poll_seconds=0.01,
        startup_timeout_seconds=1,
        popen=fake_popen,
    )
    worker.start("restart-child")
    assert len(children) == 1

    children[0].returncode = 23
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = journal.load("restart-child")
        if len(children) == 2 and record.get("supervisor_child_status") == "running":
            break
        time.sleep(0.01)
    record = journal.load("restart-child")
    assert len(children) == 2
    assert record["supervisor_child_restart_count"] == 1
    assert record["supervisor_child_status"] == "running"
    assert record["supervisor_child_exit_code"] == 23

    with journal.locked("restart-child"):
        record = journal.load("restart-child")
        record["state"] = "DESTROYED"
        journal.save(record)
    children[1].returncode = 0

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = journal.load("restart-child")
        if record.get("supervisor_child_status") == "stopped_terminal":
            break
        time.sleep(0.01)
    assert journal.load("restart-child")["supervisor_child_status"] == "stopped_terminal"
    assert len(children) == 2
