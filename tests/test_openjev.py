from __future__ import annotations

import base64
import hashlib
import json
import random

import pytest

from vast_broker.openjev import (
    OPENJEV_COMMIT,
    OPENJEV_MODEL_REVISION,
    QWEN_BASE_REVISION,
    OpenJevTrialOperation,
    deployment_recipe,
    lease_plan_from_proposal,
    systemone_request,
    trial_steps,
    validate_probe_response,
    _validate_gpu_preflight,
)


def _response(probabilities=None, *, choice="billing"):
    return {
        "model": "Qwen/Qwen3.5-9B",
        "answers": {
            "intent": {
                "choice": choice,
                "probabilities": probabilities or {
                    "billing": 0.8,
                    "technical": 0.1,
                    "other": 0.1,
                },
                "confidence": 0.8,
            }
        },
        "usage": {"input_tokens": 384, "output_tokens": 0},
        "metadata": {
            "method": "lora_decision_head",
            "code_commit": OPENJEV_COMMIT,
            "base_revision": QWEN_BASE_REVISION,
            "checkpoint_sha256": "a" * 64,
            "max_length": 16384,
            "candidate_sequences": 3,
            "prefix_cache": {"enabled": True},
            "inference_seconds": 0.5,
        },
    }


def test_trial_recipe_is_pinned_deterministic_and_respects_vast_command_limit():
    first = trial_steps()
    second = trial_steps()

    assert [step.name for step in first] == [step.name for step in second]
    assert [step.command for step in first] == [step.command for step in second]
    assert {"gpu-preflight", "artifact-download", "model-health", "typed-inference"} <= {
        step.name for step in first
    }
    assert all(0 < len(step.command) <= 512 for step in first)
    assert all("127.0.0.1:8791/v1/systemone" in step.command for step in first if step.probe)
    health_step = next(step for step in first if step.name == "model-health")
    assert "127.0.0.1:8791/health" in health_step.command
    assert "/openapi.json" not in health_step.command
    assert OPENJEV_COMMIT in first[1].command
    assert OPENJEV_MODEL_REVISION in first[4].command

    recipe = deployment_recipe({"model": "Open-Jev-9B", "gpu": "RTX 5060 Ti 16 GB"}, 80)
    assert recipe["runtime"]["base_revision"] == QWEN_BASE_REVISION
    assert recipe["create_params"]["disk"] == 80
    assert recipe["create_params"]["target_state"] == "running"


@pytest.mark.parametrize("disk", [True, False, 0, 59, 60.0, "80"])
def test_recipe_rejects_implicit_or_insufficient_disk(disk):
    with pytest.raises(ValueError):
        deployment_recipe({}, disk)


def _valid_proposal():
    identity = {
        "artifact_repo": "ZefanCai/Open-Jev-9B",
        "revision": "47e966881e489511c0c7f5633a9e1960a676a551",
        "base_id": "Qwen/Qwen3.5-9B",
        "format": "safetensors",
    }
    limits = {
        "temporary_disk_gb": 80,
        "max_hourly_usd": 0.2,
        "max_total_usd": 1.0,
        "max_runtime_seconds": 1800,
        "start_deadline_seconds": 1800,
        "cold_start_timeout_seconds": 1200,
        "idle_timeout_seconds": 300,
        "hung_request_timeout_seconds": 300,
        "max_network_usd": 0.05,
    }
    return {
        "identity": identity,
        "limits": limits,
        "offer": {"id": 82, "rental_type": "ondemand", "machine_hour_usd": 0.12},
        "deployment_recipe": deployment_recipe(identity, 80),
    }


def test_lease_plan_is_derived_from_the_immutable_proposal():
    proposal = _valid_proposal()
    plan = lease_plan_from_proposal(proposal)
    assert plan["offer_id"] == 82
    assert plan["rental_type"] == "ondemand"
    assert plan["create_params"] == proposal["deployment_recipe"]["create_params"]
    assert plan["max_runtime_seconds"] == proposal["limits"]["max_runtime_seconds"]
    assert plan["recipe_digest"]

    bid_proposal = _valid_proposal()
    bid_proposal["offer"] = {
        "id": 83, "rental_type": "bid", "bid_current_machine_hour_usd": 0.08,
    }
    bid_proposal["limits"].update({
        "max_bid_usd_per_machine_hour": 0.12,
        "bid_increment_usd": 0.02,
        "max_bid_attempts": 2,
    })
    bid_plan = lease_plan_from_proposal(bid_proposal)
    assert bid_plan["create_params"]["price"] == "0.08"
    assert bid_plan["starting_bid_usd_per_machine_hour"] == 0.08


@pytest.mark.parametrize("mutation", [
    lambda p: p["identity"].update(revision="un-pinned"),
    lambda p: p["identity"].update(base_id="Qwen/Other"),
    lambda p: p["deployment_recipe"].update(image="pytorch/pytorch:latest"),
    lambda p: p["offer"].update(rental_type="reserved"),
    lambda p: p["offer"].update(id=None),
])
def test_lease_plan_rejects_mutations_outside_the_pinned_proposal(mutation):
    proposal = _valid_proposal()
    mutation(proposal)
    with pytest.raises(ValueError):
        lease_plan_from_proposal(proposal)


def test_systemone_request_is_typed_and_returned_as_a_fresh_object():
    request = systemone_request()
    assert request["questions"]["intent"]["type"] == "choice"
    assert set(request["questions"]["intent"]["criteria"]) == {
        "billing", "technical", "other"
    }
    request["questions"]["intent"]["criteria"].clear()
    assert len(systemone_request()["questions"]["intent"]["criteria"]) == 3


def test_probe_accepts_documented_response_and_records_exact_body_digest():
    raw = json.dumps(_response(), separators=(",", ":")).encode()
    result = validate_probe_response(raw)
    assert result["choice"] == "billing"
    assert result["probabilities"] == {"billing": 0.8, "technical": 0.1, "other": 0.1}
    assert result["response_sha256"]


def test_probe_rejects_malformed_or_unrelated_probability_maps():
    malformed = [
        {},
        {"metadata": {"probabilities": {"billing": 0.8, "technical": 0.1, "other": 0.1}}},
        _response({"billing": 0.8, "technical": 0.2}),
        _response({"billing": 0.8, "technical": 0.1, "other": 0.1, "extra": 0.0}),
        _response({"billing": 0.8, "technical": 0.1, "other": 0.2}),
        _response({"billing": -0.1, "technical": 0.6, "other": 0.5}),
        _response({"billing": float("nan"), "technical": 0.0, "other": 1.0}),
        _response({"billing": True, "technical": 0.0, "other": 0.0}),
        _response({"billing": "0.8", "technical": 0.1, "other": 0.1}),
        _response(choice="unlisted"),
        _response({"billing": 0.1, "technical": 0.8, "other": 0.1}, choice="billing"),
    ]
    for response in malformed:
        with pytest.raises(ValueError):
            validate_probe_response(json.dumps(response))

    for raw in (b"", b"\xff", b'{"x":NaN}', b'{"x":1,"x":2}', None, 17):
        with pytest.raises(ValueError):
            validate_probe_response(raw)

    for field, wrong_value in (
        ("code_commit", "f" * 40),
        ("base_revision", "f" * 40),
        ("method", "pretrained_yes_minus_no_no_training"),
        ("checkpoint_sha256", "not-a-digest"),
    ):
        response = _response()
        response["metadata"][field] = wrong_value
        with pytest.raises(ValueError):
            validate_probe_response(json.dumps(response))

    for field, wrong_value in (
        ("max_length", 4096),
        ("candidate_sequences", 2),
        ("prefix_cache", {"enabled": False}),
        ("inference_seconds", 0),
        ("inference_seconds", float("nan")),
    ):
        response = _response()
        response["metadata"][field] = wrong_value
        with pytest.raises(ValueError):
            validate_probe_response(json.dumps(response))


@pytest.mark.parametrize("health", [
    {"status": "ready", "model": "Qwen/Qwen3.5-9B", "method": "lora_decision_head"},
])
def test_health_response_confirms_loaded_model(health):
    from vast_broker.openjev import validate_health_response

    assert validate_health_response(json.dumps(health)) == health


@pytest.mark.parametrize("health", [
    {"status": "starting", "model": "Qwen/Qwen3.5-9B", "method": "lora_decision_head"},
    {"status": "ready", "model": "Qwen/Qwen3.5-2B", "method": "lora_decision_head"},
    {"status": "ready", "model": "Qwen/Qwen3.5-9B", "method": "pretrained_yes_minus_no_no_training"},
])
def test_health_response_rejects_wrong_loaded_model(health):
    from vast_broker.openjev import validate_health_response

    with pytest.raises(ValueError):
        validate_health_response(json.dumps(health))


def test_seeded_probability_mutations_never_pass_without_a_normalized_declared_answer():
    rng = random.Random(90628)
    base = _response()
    mutations = []
    for _ in range(500):
        candidate = json.loads(json.dumps(base))
        probabilities = candidate["answers"]["intent"]["probabilities"]
        key = rng.choice(tuple(probabilities))
        probabilities[key] = rng.choice([-0.01, 1.01, "0.5", True, None])
        mutations.append(candidate)
    for candidate in mutations:
        with pytest.raises(ValueError):
            validate_probe_response(json.dumps(candidate))


@pytest.mark.parametrize("gpu_line", [
    "RTX 5060 Ti, 16311 MiB, 0",
    "NVIDIA GeForce RTX 5060 Ti, 16311 MiB, 0",
])
def test_gpu_preflight_accepts_source_tested_single_gpu_name_forms(gpu_line):
    _validate_gpu_preflight("VBR_EXIT_CODE=0\n" + gpu_line + "\nFilesystem Size Used Avail Use% Mounted on\n")


@pytest.mark.parametrize("gpu_lines", [
    "NVIDIA GeForce RTX 5060 Ti, 16311 MiB, 0\nNVIDIA GeForce RTX 5060 Ti, 16311 MiB, 1",
    "NVIDIA GeForce RTX 5070, 16311 MiB, 0",
    "NVIDIA GeForce RTX 5060 Ti, 14999 MiB, 0",
    "NVIDIA GeForce RTX 5060 Ti, 17001 MiB, 0",
])
def test_gpu_preflight_rejects_wrong_or_ambiguous_gpu_profile(gpu_lines):
    with pytest.raises(RuntimeError):
        _validate_gpu_preflight("VBR_EXIT_CODE=0\n" + gpu_lines + "\n")


class _FakeExecutor:
    def __init__(self, *, fail_step=None):
        self.commands = []
        self.results = {}
        self.fail_step = fail_step

    def execute_instance(self, instance_id, command):
        assert instance_id == 123
        index = len(self.commands)
        self.commands.append(command)
        step = trial_steps()[index]
        exit_code = 1 if step.name == self.fail_step else 0
        output = f"VBR_EXIT_CODE={exit_code}\n"
        if step.name == "gpu-preflight":
            output += "NVIDIA GeForce RTX 5060 Ti, 16311 MiB, 0\n"
        if step.name == "model-health" and exit_code == 0:
            health = {"status": "ready", "model": "Qwen/Qwen3.5-9B",
                      "method": "lora_decision_head"}
            encoded = base64.b64encode(json.dumps(health).encode()).decode()
            output += f"VBR_HEALTH_BASE64={encoded}\n"
        if step.probe and exit_code == 0:
            encoded = base64.b64encode(json.dumps(_response()).encode()).decode()
            output += f"VBR_PROBE_BASE64={encoded}\n"
        url = f"https://s3.amazonaws.com/vast-test/result-{index}"
        self.results[url] = output
        return {"result_url": url}

    def read_instance_command_result(self, result_url, *, max_bytes=1_000_000):
        return self.results[result_url]


def test_trial_operation_runs_pinned_steps_and_requires_typed_inference():
    executor = _FakeExecutor()
    result = OpenJevTrialOperation(executor)(
        {"id": 123, "gpu_name": "NVIDIA GeForce RTX 5060 Ti"}
    )
    assert result["state"] == "inference_verified"
    assert result["instance_id"] == "123"
    assert result["choice"] == "billing"
    assert result["model_identity"]["code_commit"] == OPENJEV_COMMIT
    assert len(executor.commands) == len(trial_steps())


def test_trial_operation_stops_on_a_failed_setup_step():
    executor = _FakeExecutor(fail_step="artifact-download")
    with pytest.raises(RuntimeError, match="artifact-download"):
        OpenJevTrialOperation(executor)({"id": 123})
    assert len(executor.commands) == 5


def test_cli_report_requires_the_same_inference_attempt_and_complete_destroy_receipt():
    from copy import deepcopy

    from vast_broker.cli import _trial_report

    route = {"request_id": "trial-1", "proposal_digest": "proposal-digest"}
    lease = {"state": "DESTROYED", "instance": {"id": 123}}
    checked_probe = validate_probe_response(json.dumps(_response()))
    step_receipts = {step.name: "d" * 64 for step in trial_steps()}
    step_receipts["model_identity"] = json.dumps({
        "status": "ready", "model": "Qwen/Qwen3.5-9B", "method": "lora_decision_head",
    }, sort_keys=True)
    record = {
        "request_id": "trial-1",
        "state": "DESTROYED",
        "plan": {"proposal_digest": "proposal-digest"},
        "operation_completed_at_utc": "2026-09-28T00:00:00+00:00",
        "operation_result": {
            "state": "inference_verified", "instance_id": "123",
            "step_receipts": step_receipts, **checked_probe,
        },
        "operation_result_sha256": "",
        "confirmed_absent_at_utc": "2026-09-28T00:02:00+00:00",
        "absence_evidence": {
            "checked_at_utc": "2026-09-28T00:01:00+00:00",
            "owned_instance_ids": ["123"],
            "owned_volume_ids": ["901"],
            "owned_instance_ids_absent": True,
            "owned_volume_ids_absent": True,
            "instance_inventory": {"complete": True, "instance_count": 0,
                                    "instance_ids_sha256": "c" * 64},
        },
    }
    record["operation_result_sha256"] = hashlib.sha256(json.dumps(
        record["operation_result"], ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    report = _trial_report(route, lease, record)
    assert report["state"] == "OPENJEV_LIVE_TEST_PASSED"
    journal_roundtrip = json.loads(json.dumps(record, sort_keys=True))
    assert _trial_report(route, lease, journal_roundtrip)["state"] == "OPENJEV_LIVE_TEST_PASSED"

    mutations = [
        lambda item: item.update(state="CLEANUP_PENDING"),
        lambda item: item.update(request_id="another-trial"),
        lambda item: item["plan"].update(proposal_digest="different"),
        lambda item: item["absence_evidence"].update(owned_instance_ids=["123", "124"]),
        lambda item: item["absence_evidence"].update(owned_instance_ids_absent=False),
        lambda item: item["absence_evidence"].update(owned_volume_ids_absent=False),
        lambda item: item["absence_evidence"].update(owned_volume_ids=None),
        lambda item: item["absence_evidence"]["instance_inventory"].update(complete=False),
        lambda item: item["absence_evidence"]["instance_inventory"].update(instance_count=-1),
        lambda item: item["absence_evidence"]["instance_inventory"].update(instance_ids_sha256="bad"),
        lambda item: item.pop("confirmed_absent_at_utc"),
        lambda item: item["absence_evidence"].update(checked_at_utc="not-a-time"),
        lambda item: item["operation_result"].update(instance_id="999"),
        lambda item: item["operation_result"].update(state="setup_failed"),
        lambda item: item["operation_result"].update(choice="technical"),
        lambda item: item["operation_result"]["step_receipts"].pop("gpu-preflight"),
        lambda item: item["operation_result"]["response"].update(model="Qwen/Qwen3.5-2B"),
        lambda item: item["operation_result"]["step_receipts"].update(model_identity="{}"),
        lambda item: item.update(operation_result_sha256="0" * 64),
    ]
    for mutate in mutations:
        altered = deepcopy(record)
        mutate(altered)
        assert _trial_report(route, lease, altered)["state"] == "OPENJEV_LIVE_TEST_INCOMPLETE"

    rng = random.Random(90629)
    for _ in range(500):
        altered = deepcopy(record)
        rng.choice(mutations)(altered)
        # Preserve a valid digest for semantic result mutations so both proof
        # layers are exercised independently.
        if altered["operation_result"] != record["operation_result"]:
            altered["operation_result_sha256"] = hashlib.sha256(json.dumps(
                altered["operation_result"], ensure_ascii=False, allow_nan=False,
                separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")).hexdigest()
        assert _trial_report(route, lease, altered)["state"] == "OPENJEV_LIVE_TEST_INCOMPLETE"


def test_cli_enforces_the_owner_hourly_cap_before_any_market_or_create_call(tmp_path, monkeypatch):
    from argparse import Namespace

    from vast_broker.cli import _run_openjev

    request_path = tmp_path / "request.json"
    evidence_path = tmp_path / "evidence.json"
    limits_path = tmp_path / "limits.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(json.dumps({"request_id": "cap-test"}), encoding="utf-8")
    evidence_path.write_text("{}", encoding="utf-8")
    limits_path.write_text(json.dumps({"max_hourly_usd": 0.21}), encoding="utf-8")
    for key in (
        "VAST_BROKER_SEARCH_API_KEY", "VAST_BROKER_LEASE_API_KEY",
        "VAST_BROKER_CONTROL_HOST_ID", "VAST_BROKER_JOURNAL_DIR",
        "VAST_BROKER_GUARDIAN_GATE_FACTORY",
    ):
        monkeypatch.delenv(key, raising=False)

    args = Namespace(
        request=str(request_path), evidence=str(evidence_path), limits=str(limits_path),
        output=str(output_path), proposal_output=str(tmp_path / "proposal.json"),
    )
    assert _run_openjev(args) == 2
    blocked = json.loads(output_path.read_text(encoding="utf-8"))
    assert blocked["state"] == "BLOCKED_OWNER_HOURLY_CAP"
    assert not (tmp_path / "proposal.json").exists()
