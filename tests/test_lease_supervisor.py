from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

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
