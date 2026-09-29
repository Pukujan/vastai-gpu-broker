"""Pinned, typed Open-Jev 9B trial recipe for a single Vast GPU lease."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


OPENJEV_REPOSITORY = "https://github.com/Zefan-Cai/Open-Jev.git"
OPENJEV_COMMIT = "3308a15ccd7eea1df7a37d6ddc39b023b801ba16"
OPENJEV_MODEL_REPOSITORY = "ZefanCai/Open-Jev-9B"
OPENJEV_MODEL_REVISION = "47e966881e489511c0c7f5633a9e1960a676a551"
QWEN_BASE_REPOSITORY = "Qwen/Qwen3.5-9B"
QWEN_BASE_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
PYTORCH_IMAGE = (
    "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
)

_CRITERIA = {
    "billing": "Payment issue",
    "technical": "Setup issue",
    "other": "Other request",
}
_REQUEST = {
    "state": "Charged twice; asks for a refund.",
    "questions": {
        "intent": {
            "type": "choice",
            "instructions": "Classify the request.",
            "criteria": _CRITERIA,
        }
    },
}


class CommandExecutor(Protocol):
    def execute_instance(self, instance_id: int, command: str) -> dict[str, Any]: ...
    def read_instance_command_result(self, result_url: str, *, max_bytes: int = 1_000_000) -> str: ...


@dataclass(frozen=True, slots=True)
class TrialStep:
    name: str
    command: str
    log_path: str
    probe: bool = False


def systemone_request() -> dict[str, Any]:
    """Return a deterministic, non-sensitive typed decision request."""
    return json.loads(json.dumps(_REQUEST, sort_keys=True))


def _run_logged(name: str, body: str, *, log_path: str, probe: bool = False,
                capture_path: str | None = None, capture_tag: str | None = None) -> TrialStep:
    if not body or "\x00" in body:
        raise ValueError("trial command is malformed")
    tail = "tail -n 60 " + log_path
    output = f"{tail}; "
    if probe:
        output += (
            "if [ \"$code\" -eq 0 ]; then printf '\\nVBR_PROBE_BASE64='; "
            "base64 -w0 /workspace/openjev-response.json; printf '\\n'; fi; "
        )
    elif capture_path and capture_tag:
        output += (
            f"if [ \"$code\" -eq 0 ]; then printf '\\n{capture_tag}='; "
            f"base64 -w0 {capture_path}; printf '\\n'; fi; "
        )
    shell_script = (
        f"{body} > {log_path} 2>&1; code=$?; "
        "printf '\\nVBR_EXIT_CODE=%s\\n' \"$code\"; " + output + "exit 0"
    )
    command = "bash -lc " + shlex.quote(shell_script)
    if len(command) > 512:
        raise ValueError(f"pinned {name} command exceeds Vast's 512-character limit")
    return TrialStep(name, command, log_path, probe)


def trial_steps() -> tuple[TrialStep, ...]:
    """Build fixed commands; no model-card text or caller text enters a shell."""
    request_b64 = base64.b64encode(
        json.dumps(_REQUEST, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    gpu_preflight = (
        "nvidia-smi --query-gpu=name,memory.total,index --format=csv,noheader && "
        "df -BG /workspace | tail -n 1"
    )
    checkout = (
        f"git clone --filter=blob:none {OPENJEV_REPOSITORY} /workspace/open-jev && "
        f"cd /workspace/open-jev && git fetch --depth 1 origin {OPENJEV_COMMIT} && "
        f"git checkout --detach {OPENJEV_COMMIT}"
    )
    install = "cd /workspace/open-jev && python -m pip install -e '.[train]'"
    align_accelerate = "python -m pip install --no-deps 'accelerate==1.15.0'"
    download = (
        f"hf download {OPENJEV_MODEL_REPOSITORY} --revision {OPENJEV_MODEL_REVISION} "
        "--local-dir /workspace/openjev-checkpoint"
    )
    device_map = json.dumps({
        "model.visual": "cpu",
        "lm_head": "cpu",
        "model.language_model.embed_tokens": "cpu",
        "model.language_model.rotary_emb": 0,
        "model.language_model": 0,
    }, separators=(",", ":"))
    environment = (
        f"JEV_DEVICE_MAP='{device_map}'\n"
        "JEV_PREFILL_CHUNK=256\nJEV_RAGGED_SUFFIX=1\n"
    )
    environment_b64 = base64.b64encode(environment.encode("utf-8")).decode("ascii")
    write_environment = (
        f"printf %s {environment_b64} | base64 -d > /workspace/openjev.env"
    )
    start_server = (
        "cd /workspace/open-jev && nohup bash -lc 'set -a; "
        ". /workspace/openjev.env; set +a; "
        "python -m jev.server --checkpoint /workspace/openjev-checkpoint/package/checkpoint "
        "--device cuda:0 --batch-size 2 --max-length 16384 --prefix-cache "
        "--host 127.0.0.1 --port 8791' > /workspace/openjev-server.log 2>&1 & "
        "echo $! > /workspace/openjev-server.pid"
    )
    health = (
        "for n in $(seq 1 900); do "
        "curl -fsS http://127.0.0.1:8791/health -o /workspace/openjev-health.json && exit 0; "
        "sleep 2; done; exit 1"
    )
    write_request = (
        f"printf %s {request_b64} | base64 -d > /workspace/openjev-request.json"
    )
    probe = (
        "curl -fsS -H 'Content-Type: application/json' "
        "--data-binary @/workspace/openjev-request.json "
        "http://127.0.0.1:8791/v1/systemone -o /workspace/openjev-response.json"
    )
    steps = (
        _run_logged("gpu-preflight", gpu_preflight, log_path="/workspace/vbr-gpu.log"),
        _run_logged("source-checkout", checkout, log_path="/workspace/vbr-checkout.log"),
        _run_logged("runtime-install", install, log_path="/workspace/vbr-install.log"),
        _run_logged("runtime-pin", align_accelerate, log_path="/workspace/vbr-runtime-pin.log"),
        _run_logged("artifact-download", download, log_path="/workspace/vbr-download.log"),
        _run_logged("model-environment", write_environment, log_path="/workspace/vbr-environment.log"),
        _run_logged("model-start", start_server, log_path="/workspace/vbr-start.log"),
        _run_logged("model-health", health, log_path="/workspace/vbr-health.log",
                    capture_path="/workspace/openjev-health.json", capture_tag="VBR_HEALTH_BASE64"),
        _run_logged("typed-request", write_request, log_path="/workspace/vbr-request.log"),
        _run_logged("typed-inference", probe,
                    log_path="/workspace/vbr-inference.log", probe=True),
    )
    return steps


def deployment_recipe(identity: Mapping[str, Any], disk_gb: int) -> dict[str, Any]:
    """Return the exact pinned image, artifact, and launch profile for the plan."""
    if isinstance(disk_gb, bool) or not isinstance(disk_gb, int) or disk_gb < 60:
        raise ValueError("Open-Jev 9B recipe requires an explicit disk allocation of at least 60 GB")
    params = {
        "disk": disk_gb,
        "target_state": "running",
        "cancel_unavail": True,
        "runtype": "args",
        "args": ["bash", "-lc", "sleep infinity"],
    }
    recipe = {
        "identity": dict(identity),
        "image": PYTORCH_IMAGE,
        "create_params": params,
        "runtime": {
            "repository": OPENJEV_REPOSITORY,
            "commit": OPENJEV_COMMIT,
            "python_package": ".[train]",
            "torch_image": PYTORCH_IMAGE,
            "accelerate": "1.15.0",
            "transformers": "5.10.2",
            "peft": "0.19.1",
            "artifact_repository": OPENJEV_MODEL_REPOSITORY,
            "artifact_revision": OPENJEV_MODEL_REVISION,
            "base_repository": QWEN_BASE_REPOSITORY,
            "base_revision": QWEN_BASE_REVISION,
            "device_map": {
                "model.visual": "cpu",
                "lm_head": "cpu",
                "model.language_model.embed_tokens": "cpu",
                "model.language_model.rotary_emb": 0,
                "model.language_model": 0,
            },
            "prefill_chunk": 256,
            "ragged_suffix": True,
            "batch_size": 2,
            "max_length": 16384,
            "prefix_cache": True,
            "systemone_endpoint": "http://127.0.0.1:8791/v1/systemone",
        },
        "steps": [
            {"name": step.name,
             "command_sha256": hashlib.sha256(step.command.encode("utf-8")).hexdigest()}
            for step in trial_steps()
        ],
        "probe_request_sha256": hashlib.sha256(
            json.dumps(_REQUEST, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    return recipe


def lease_plan_from_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Bind an Open-Jev proposal to the exact controller lease plan."""
    identity = proposal.get("identity")
    limits = proposal.get("limits")
    offer = proposal.get("offer")
    recipe = proposal.get("deployment_recipe")
    if not all(isinstance(value, Mapping) for value in (identity, limits, offer, recipe)):
        raise ValueError("Open-Jev proposal omitted its identity, limits, offer or recipe")
    if (identity.get("artifact_repo") != OPENJEV_MODEL_REPOSITORY
            or identity.get("revision") != OPENJEV_MODEL_REVISION
            or identity.get("base_id") != QWEN_BASE_REPOSITORY):
        raise ValueError("proposal does not select the pinned Open-Jev 9B adapter and Qwen base")
    disk = limits.get("temporary_disk_gb")
    if isinstance(disk, bool) or not isinstance(disk, int):
        raise ValueError("Open-Jev proposal must authorize an integer temporary disk allocation")
    expected_recipe = deployment_recipe(identity, disk)
    if dict(recipe) != expected_recipe:
        raise ValueError("proposal runtime differs from the pinned Open-Jev trial recipe")

    rental_type = offer.get("rental_type")
    if rental_type not in {"ondemand", "bid"} or offer.get("id") is None:
        raise ValueError("Open-Jev proposal must select a direct on-demand or bounded bid offer")
    create_params = dict(expected_recipe["create_params"])
    plan: dict[str, Any] = {
        "offer_id": offer["id"],
        "rental_type": rental_type,
        "identity": dict(identity),
        "recipe_digest": hashlib.sha256(
            json.dumps(expected_recipe, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest(),
        "create_params": create_params,
        "max_runtime_seconds": limits.get("max_runtime_seconds"),
        "start_deadline_seconds": limits.get("start_deadline_seconds"),
        "cold_start_timeout_seconds": limits.get("cold_start_timeout_seconds"),
        "idle_timeout_seconds": limits.get("idle_timeout_seconds"),
        "hung_request_timeout_seconds": limits.get("hung_request_timeout_seconds"),
        "max_network_usd": limits.get("max_network_usd"),
    }
    if rental_type == "bid":
        bid = offer.get("bid_current_machine_hour_usd")
        if bid is None:
            raise ValueError("bounded bid offer omitted its current machine-hour quote")
        plan.update({
            "max_bid_usd_per_machine_hour": limits.get("max_bid_usd_per_machine_hour"),
            "bid_increment_usd": limits.get("bid_increment_usd"),
            "max_bid_attempts": limits.get("max_bid_attempts"),
            "starting_bid_usd_per_machine_hour": bid,
        })
        create_params["price"] = str(bid)
    return plan


def validate_probe_response(payload: bytes | str) -> dict[str, Any]:
    """Require the documented answer for this exact typed choice request."""
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise ValueError("Open-Jev probe response must be bytes or text")
    if not isinstance(raw, bytes) or not raw or len(raw) > 1_000_000:
        raise ValueError("Open-Jev probe response is empty or exceeds the result limit")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        response = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON number")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("Open-Jev probe did not return valid UTF-8 JSON") from None

    answers = response.get("answers") if isinstance(response, Mapping) else None
    answer = answers.get("intent") if isinstance(answers, Mapping) else None
    probability_map = answer.get("probabilities") if isinstance(answer, Mapping) else None
    choice = answer.get("choice") if isinstance(answer, Mapping) else None
    if (not isinstance(probability_map, Mapping)
            or set(probability_map) != set(_CRITERIA)
            or not isinstance(choice, str)
            or choice not in _CRITERIA):
        raise ValueError("Open-Jev probe response omitted the declared typed choice answer")
    probabilities: dict[str, float] = {}
    for name in _CRITERIA:
        number = probability_map[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("Open-Jev probe returned a non-numeric choice probability")
        converted = float(number)
        if not math.isfinite(converted) or not 0 <= converted <= 1:
            raise ValueError("Open-Jev probe returned an invalid choice probability")
        probabilities[name] = converted
    if not math.isclose(sum(probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-5):
        raise ValueError("Open-Jev probe probabilities did not form a normalized distribution")
    if probabilities[choice] < max(probabilities.values()):
        raise ValueError("Open-Jev selected choice did not match its probability distribution")
    metadata = response.get("metadata")
    usage = response.get("usage")
    prefix_cache = metadata.get("prefix_cache") if isinstance(metadata, Mapping) else None
    inference_seconds = metadata.get("inference_seconds") if isinstance(metadata, Mapping) else None
    input_tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
    if (not isinstance(metadata, Mapping)
            or response.get("model") != QWEN_BASE_REPOSITORY
            or metadata.get("method") != "lora_decision_head"
            or metadata.get("base_revision") != QWEN_BASE_REVISION
            or metadata.get("code_commit") != OPENJEV_COMMIT
            or metadata.get("max_length") != 16384
            or metadata.get("candidate_sequences") != len(_CRITERIA)
            or not isinstance(prefix_cache, Mapping)
            or prefix_cache.get("enabled") is not True
            or isinstance(inference_seconds, bool)
            or not isinstance(inference_seconds, (int, float))
            or not math.isfinite(inference_seconds)
            or inference_seconds <= 0
            or isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens <= 0
            or usage.get("output_tokens") != 0
            or not isinstance(metadata.get("checkpoint_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["checkpoint_sha256"])):
        raise ValueError("Open-Jev probe did not attest the pinned 9B adapter, base and loader")
    return {
        "response": response,
        "choice": choice,
        "probabilities": probabilities,
        "model_identity": {
            "model": response["model"],
            "method": metadata["method"],
            "code_commit": metadata["code_commit"],
            "base_revision": metadata["base_revision"],
            "checkpoint_sha256": metadata["checkpoint_sha256"],
            "max_length": metadata["max_length"],
            "candidate_sequences": metadata["candidate_sequences"],
            "prefix_cache": dict(prefix_cache),
            "inference_seconds": float(inference_seconds),
            "input_tokens": input_tokens,
        },
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }


def validate_health_response(payload: bytes | str) -> dict[str, str]:
    """Require the pinned server's readiness route to report its loaded model."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes) or not raw or len(raw) > 64_000:
        raise ValueError("Open-Jev health response is empty or exceeds the result limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Open-Jev health response was not valid UTF-8 JSON") from None
    if (not isinstance(value, Mapping)
            or value.get("status") != "ready"
            or value.get("model") != QWEN_BASE_REPOSITORY
            or value.get("method") != "lora_decision_head"):
        raise ValueError("Open-Jev health route did not confirm the expected loaded model")
    return {"model": value["model"], "method": value["method"], "status": value["status"]}


class OpenJevTrialOperation:
    """Execute the fixed recipe through Vast's bounded remote-command API."""

    def __init__(self, executor: CommandExecutor, *, clock: Callable[[], float] = time.monotonic,
                 sleep_fn: Callable[[float], None] = time.sleep, step_timeout_seconds: float = 2700.0,
                 poll_seconds: float = 2.0):
        if not math.isfinite(step_timeout_seconds) or step_timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("trial command deadlines must be finite and positive")
        self.executor, self.clock, self.sleep_fn = executor, clock, sleep_fn
        self.step_timeout_seconds, self.poll_seconds = step_timeout_seconds, poll_seconds

    def __call__(self, instance: Mapping[str, Any]) -> dict[str, Any]:
        instance_id = instance.get("id", instance.get("new_contract"))
        try:
            numeric_id = int(instance_id)
        except (TypeError, ValueError):
            raise ValueError("Vast create result omitted a valid instance ID") from None
        if numeric_id < 1 or str(numeric_id) != str(instance_id):
            raise ValueError("Vast create result omitted a valid instance ID")
        evidence: dict[str, str] = {}
        for step in trial_steps():
            queued = self.executor.execute_instance(numeric_id, step.command)
            result_url = queued.get("result_url") if isinstance(queued, Mapping) else None
            if not isinstance(result_url, str) or not result_url:
                raise RuntimeError("Vast did not return a command result receipt")
            output = self._wait_for_result(result_url)
            match = re.search(r"(?:^|\n)VBR_EXIT_CODE=(\d+)(?:\r?\n|$)", output)
            if not match or int(match.group(1)) != 0:
                raise RuntimeError(f"Open-Jev trial step failed: {step.name}")
            if step.name == "gpu-preflight":
                _validate_gpu_preflight(output)
            evidence[step.name] = hashlib.sha256(output.encode("utf-8")).hexdigest()
            if step.name == "model-health":
                encoded = re.search(r"(?:^|\n)VBR_HEALTH_BASE64=([A-Za-z0-9+/=]+)(?:\r?\n|$)", output)
                if not encoded:
                    raise RuntimeError("Open-Jev server readiness returned no bounded health payload")
                try:
                    health_payload = base64.b64decode(encoded.group(1), validate=True)
                except (ValueError, base64.binascii.Error):
                    raise RuntimeError("Open-Jev health response encoding was invalid") from None
                try:
                    evidence["model_identity"] = json.dumps(
                        validate_health_response(health_payload), sort_keys=True
                    )
                except ValueError as exc:
                    raise RuntimeError("Open-Jev server did not load the pinned adapter") from exc
            if step.probe:
                encoded = re.search(r"(?:^|\n)VBR_PROBE_BASE64=([A-Za-z0-9+/=]+)(?:\r?\n|$)", output)
                if not encoded:
                    raise RuntimeError("Open-Jev inference returned no bounded response payload")
                try:
                    decoded = base64.b64decode(encoded.group(1), validate=True)
                except (ValueError, base64.binascii.Error):
                    raise RuntimeError("Open-Jev inference response encoding was invalid") from None
                checked = validate_probe_response(decoded)
                return {"state": "inference_verified", "instance_id": str(numeric_id),
                        "step_receipts": evidence, **checked}
        raise RuntimeError("Open-Jev recipe ended without its typed inference step")

    def _wait_for_result(self, result_url: str) -> str:
        deadline = self.clock() + self.step_timeout_seconds
        while self.clock() < deadline:
            try:
                return self.executor.read_instance_command_result(result_url)
            except Exception as exc:
                # Vast's command endpoint returns its result asynchronously. A
                # not-yet-written log is retried; other errors fail into lease cleanup.
                if "HTTP 403" not in str(exc) and "HTTP 404" not in str(exc):
                    raise RuntimeError("Vast command result could not be read") from None
                self.sleep_fn(min(self.poll_seconds, max(0.0, deadline - self.clock())))
        raise TimeoutError("Vast command result exceeded its bounded step deadline")


def _validate_gpu_preflight(output: str) -> None:
    rows = re.findall(r"(?im)^\s*([^,\r\n]+),\s*([\d,]+)\s*MiB,\s*(\d+)\s*$", output)
    if len(rows) != 1:
        raise RuntimeError("Open-Jev trial requires exactly one visible GPU")
    name, raw_memory, _index = rows[0]
    memory_mb = int(raw_memory.replace(",", ""))
    if (not re.fullmatch(r"(?i)(?:NVIDIA\s+)?(?:GeForce\s+)?RTX 5060 Ti", name.strip())
            or not 15_000 <= memory_mb <= 17_000):
        raise RuntimeError("rented GPU did not match the source-tested RTX 5060 Ti 16 GB profile")
