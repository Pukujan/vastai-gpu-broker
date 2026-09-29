from __future__ import annotations

import json

from vast_broker import cli
from vast_broker.journal import LeaseJournal


class MemoryProvider:
    def __init__(self):
        self.instances = {}
        self.volumes = {}
        self.destroyed = []

    def list_instances(self):
        return [dict(row) for row in self.instances.values()]

    def list_volumes(self):
        return [
            {"id": volume_id, "instances": [dict(item) for item in row.get("instances", [])]}
            for volume_id, row in self.volumes.items()
        ]

    def destroy_instance(self, instance_id):
        self.destroyed.append(str(instance_id))
        self.instances.pop(str(instance_id), None)
        return {"success": True}

    def destroy_volume(self, volume_id):
        self.volumes.pop(str(volume_id), None)
        return {"success": True}


def _saved_lease(journal_dir, request_id="trial-1", instance_id="42"):
    journal = LeaseJournal(journal_dir)
    journal.save({
        "request_id": request_id,
        "state": "READY",
        "owned_instance_ids": [instance_id],
        "owned_volume_ids": [],
        "unresolved_volume_ids": [],
        "pre_create_volume_ids": [],
        "pre_create_instance_ids": [],
        "volume_discovery_pending": False,
        "instance": {"id": instance_id, "volume_ids": []},
        "operation_result": {"state": "inference_verified"},
    })
    return journal


def test_search_and_offers_aliases_share_identical_search_arguments():
    parser = cli._parser()
    search = vars(parser.parse_args(["search", "--page-size", "7"]))
    offers = vars(parser.parse_args(["offers", "--page-size", "7"]))
    search.pop("command")
    offers.pop("command")
    assert search == offers


def test_create_and_run_openjev_aliases_use_the_same_guarded_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_run_openjev", lambda args: calls.append(vars(args)) or 0)
    argv = ["--request", "request.json", "--evidence", "evidence.json", "--limits", "limits.json",
            "--proposal-output", "proposal.json", "--output", "result.json"]
    assert cli.main(["create", *argv]) == 0
    assert cli.main(["run-openjev", *argv]) == 0
    assert calls[0]["command"] == "create"
    assert calls[1]["command"] == "run-openjev"
    for key in calls[0]:
        if key != "command":
            assert calls[0][key] == calls[1][key]


def test_status_refreshes_only_owned_rows_and_omits_connection_secrets(tmp_path, monkeypatch, capsys):
    request_id = "trial-status"
    journal = _saved_lease(tmp_path, request_id)
    provider = MemoryProvider()
    provider.instances = {
        "42": {"id": 42, "label": "broker-trial", "actual_status": "running", "ssh_host": "private.host"},
        "99": {"id": 99, "actual_status": "running"},
    }
    monkeypatch.setenv("VAST_BROKER_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "lease_provider_factory", lambda: provider)

    assert cli.main(["status", request_id]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["state"] == "READY"
    assert payload["instances"] == [{"id": 42, "actual_status": "running", "label": "broker-trial"}]
    assert payload["owned_instances_absent"] is False
    assert "private.host" not in output
    assert journal.load(request_id)["state"] == "READY"


def test_destroy_removes_only_journaled_resources_and_verifies_absence(tmp_path, monkeypatch, capsys):
    request_id = "trial-destroy"
    _saved_lease(tmp_path, request_id)
    provider = MemoryProvider()
    provider.instances = {
        "42": {"id": "42", "label": "broker-trial", "status": "running"},
        "99": {"id": "99", "label": "unrelated", "status": "running"},
    }
    monkeypatch.setenv("VAST_BROKER_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "lease_provider_factory", lambda: provider)

    assert cli.main(["destroy", request_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["destroyed"] is True
    assert payload["cleanup_verified"] is True
    assert provider.destroyed == ["42"]
    assert set(provider.instances) == {"99"}
    assert LeaseJournal(tmp_path).load(request_id)["state"] == "DESTROYED"


def test_destroy_rejects_untracked_request_without_contacting_vast(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VAST_BROKER_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "lease_provider_factory", lambda: (_ for _ in ()).throw(AssertionError("provider call")))

    assert cli.main(["destroy", "not-owned-by-broker"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"destroyed": False, "request_id": "not-owned-by-broker", "state": "NOT_FOUND"}


def test_status_never_calls_inventory_failure_absence(tmp_path, monkeypatch, capsys):
    request_id = "trial-provider-error"
    _saved_lease(tmp_path, request_id)

    class UnavailableProvider(MemoryProvider):
        def list_instances(self):
            raise TimeoutError("provider unavailable")

    monkeypatch.setenv("VAST_BROKER_JOURNAL_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "lease_provider_factory", UnavailableProvider)
    assert cli.main(["status", request_id]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["instance_inventory_complete"] is False
    assert payload["owned_instances_absent"] is None
    assert payload["cleanup_verified"] is False
