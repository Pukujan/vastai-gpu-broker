"""Fail-closed checks for the repository's pinned CGM adopter overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


EXPECTED_REPOSITORY = "https://github.com/Pukujan/content-generation-modules"
EXPECTED_MODULES = {
    "brand-foundation",
    "content-context",
    "writing-direction",
    "human-sounding-writing",
    "human-output-naming",
    "visual-direction",
    "image-generation",
    "html-demo",
}
INJECT_START = "<!-- cgm:always-on-start -->"
INJECT_END = "<!-- cgm:always-on-end -->"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def validate_adoption(project_root: Path, helper_root: Path, source_revision: str | None = None) -> list[str]:
    project_root = project_root.resolve()
    helper_root = helper_root.resolve()
    adapter = project_root / ".content-system"
    errors: list[str] = []

    try:
        pin = read_json(adapter / "system-version.json")
        helper_version = read_json(helper_root / "system-version.json")
        routing = read_json(helper_root / "docs" / "writing-routing.json")
    except ValueError as exc:
        return [str(exc)]

    if source_revision is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=helper_root,
                check=True,
                capture_output=True,
                text=True,
            )
            source_revision = result.stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"cannot resolve the checked-out CGM revision: {exc}")
            source_revision = ""

    if pin.get("schema_version") != "content-generation.adapter.v1":
        errors.append("adapter schema_version must be content-generation.adapter.v1")
    if pin.get("helper_repository") != EXPECTED_REPOSITORY:
        errors.append(f"helper_repository must be {EXPECTED_REPOSITORY}")
    if pin.get("helper_version") != helper_version.get("version"):
        errors.append("adapter helper_version does not match the checked-out helper")
    pin_commit = str(pin.get("helper_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", pin_commit):
        errors.append("adapter helper_commit must be a full lowercase 40-character SHA")
    if pin_commit != source_revision:
        errors.append("adapter helper_commit does not match the checked-out CGM revision")
    modules = set(pin.get("modules") or [])
    helper_modules = set(helper_version.get("modules") or [])
    if modules != EXPECTED_MODULES:
        errors.append("adapter must pin the complete eight-module CGM set")
    if helper_modules != modules:
        errors.append("adapter modules do not match the checked-out CGM helper")

    inject = routing.get("acs_prompt_inject")
    canonical_block = inject.get("system_block") if isinstance(inject, dict) else None
    if not isinstance(canonical_block, str) or not canonical_block.strip():
        errors.append("pinned helper does not provide acs_prompt_inject.system_block")
        canonical_block = ""
    try:
        stored_block = (adapter / "agent-system-block.txt").read_text(encoding="utf-8").strip()
    except OSError as exc:
        errors.append(f"cannot read adopter system block: {exc}")
        stored_block = ""
    if stored_block != canonical_block.strip():
        errors.append("adopter system block differs from the pinned helper's canonical block")

    try:
        agents = (project_root / "AGENTS.md").read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read AGENTS.md boot instructions: {exc}")
        agents = ""
    if agents.count(INJECT_START) != 1 or agents.count(INJECT_END) != 1:
        errors.append("AGENTS.md must contain one bounded CGM always-on block")
    elif INJECT_START in agents and INJECT_END in agents:
        start = agents.index(INJECT_START) + len(INJECT_START)
        end = agents.index(INJECT_END)
        if end < start or agents[start:end].strip() != canonical_block.strip():
            errors.append("AGENTS.md boot block differs from the pinned helper's canonical block")

    try:
        manifest = read_json(adapter / "asset-manifest.json")
    except ValueError as exc:
        errors.append(str(exc))
        manifest = {}
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        errors.append("adapter asset-manifest.json must list at least one target asset")
        assets = []
    for index, asset in enumerate(assets, 1):
        if not isinstance(asset, dict):
            errors.append(f"asset {index} must be a JSON object")
            continue
        for field in ("path", "hash", "prompt_record"):
            if not asset.get(field):
                errors.append(f"asset {index} is missing {field}")
        asset_path = str(asset.get("path") or "")
        if asset_path:
            resolved = (project_root / asset_path).resolve()
            try:
                resolved.relative_to(project_root)
            except ValueError:
                errors.append(f"asset path escapes the project root: {asset_path}")
            else:
                if not resolved.is_file():
                    errors.append(f"asset file does not exist: {asset_path}")
                elif hashlib.sha256(resolved.read_bytes()).hexdigest() != str(asset.get("hash", "")).lower():
                    errors.append(f"asset hash does not match the file: {asset_path}")
        record_path = str(asset.get("prompt_record") or "")
        if record_path and not (project_root / record_path).is_file():
            errors.append(f"asset source/provenance record does not exist: {record_path}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--helper-root", type=Path, required=True)
    args = parser.parse_args()
    errors = validate_adoption(args.project_root, args.helper_root)
    if errors:
        print("INVALID: CGM adoption")
        for error in errors:
            print(f"- {error}")
        return 1
    print("VALID: pinned CGM adopter overlay, boot injection, and asset provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
