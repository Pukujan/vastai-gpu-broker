import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_cgm_adoption import EXPECTED_MODULES, INJECT_END, INJECT_START, validate_adoption


COMMIT = "c069613ca8b3e02bcf5aba1960160583537f8a3a"
VERSION = "0.5.7"
BLOCK = "CGM ALWAYS-ON WRITING RULE (every adopter that pins this helper)\n\nLoad the pinned writing modules before human-facing work."


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class CGMAdoptionTests(unittest.TestCase):
    def make_project(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        helper = root / "helper"
        adapter = project / ".content-system"
        helper.mkdir(parents=True)
        image_path = project / "docs" / "content-system-assets" / "diagram.png"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"fixture PNG bytes")
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        (project / "docs" / "asset-source.json").write_text("{}", encoding="utf-8")

        write_json(
            helper / "system-version.json",
            {"version": VERSION, "modules": sorted(EXPECTED_MODULES)},
        )
        write_json(
            helper / "docs" / "writing-routing.json",
            {"acs_prompt_inject": {"system_block": BLOCK}},
        )
        write_json(
            adapter / "system-version.json",
            {
                "schema_version": "content-generation.adapter.v1",
                "helper_repository": "https://github.com/Pukujan/content-generation-modules",
                "helper_version": VERSION,
                "helper_commit": COMMIT,
                "modules": sorted(EXPECTED_MODULES),
            },
        )
        (adapter / "agent-system-block.txt").write_text(BLOCK + "\n", encoding="utf-8")
        write_json(
            adapter / "asset-manifest.json",
            {
                "assets": [
                    {
                        "path": "docs/content-system-assets/diagram.png",
                        "hash": digest,
                        "prompt_record": "docs/asset-source.json",
                    }
                ]
            },
        )
        (project / "AGENTS.md").write_text(
            f"# instructions\n{INJECT_START}\n{BLOCK}\n{INJECT_END}\n",
            encoding="utf-8",
        )
        return project, helper

    def test_valid_pin_injection_and_asset_provenance_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            project, helper = self.make_project(Path(directory))
            self.assertEqual(validate_adoption(project, helper, COMMIT), [])

    def test_drifted_pin_or_boot_block_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            project, helper = self.make_project(Path(directory))
            pin_path = project / ".content-system" / "system-version.json"
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            pin["helper_commit"] = "a" * 40
            write_json(pin_path, pin)
            (project / "AGENTS.md").write_text("# instructions\n", encoding="utf-8")
            errors = validate_adoption(project, helper, COMMIT)
            self.assertTrue(any("does not match" in item for item in errors), errors)
            self.assertTrue(any("bounded CGM always-on block" in item for item in errors), errors)

    def test_asset_hash_or_missing_source_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            project, helper = self.make_project(Path(directory))
            manifest_path = project / ".content-system" / "asset-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["assets"][0]["hash"] = "0" * 64
            manifest["assets"][0]["prompt_record"] = "docs/missing.json"
            write_json(manifest_path, manifest)
            errors = validate_adoption(project, helper, COMMIT)
            self.assertTrue(any("hash does not match" in item for item in errors), errors)
            self.assertTrue(any("record does not exist" in item for item in errors), errors)


if __name__ == "__main__":
    unittest.main()
