"""File-backed provider used only by the local supervisor subprocess test."""
import json
import os
from pathlib import Path


def _path():
    return Path(os.environ["VBR_FAKE_PROVIDER_STATE"])


def _read():
    return json.loads(_path().read_text(encoding="utf-8"))


def _write(value):
    _path().write_text(json.dumps(value), encoding="utf-8")


class FileProvider:
    def list_instances(self):
        return _read()["instances"]

    def get_instance(self, iid):
        return next((x for x in self.list_instances() if str(x["id"]) == str(iid)), None)

    def create_instance(self, offer_id, params):
        raise AssertionError("supervisor worker must never create")

    def stop_instance(self, iid):
        return {"success": True}

    def destroy_instance(self, iid):
        state = _read()
        state["instances"] = [x for x in state["instances"] if str(x["id"]) != str(iid)]
        _write(state)
        return {"success": True}

    def change_bid(self, iid, price):
        raise AssertionError("supervisor worker must never bid")


def make_provider():
    return FileProvider()
