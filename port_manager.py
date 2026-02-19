import json
import os

_FILE = os.path.expanduser("~/apps/.ports.json")
_BACKEND  = range(3000, 3025)
_FRONTEND = range(3025, 3050)


def _load() -> dict:
    if os.path.exists(_FILE):
        with open(_FILE) as f:
            return json.load(f)
    return {"allocated": {}}


def _save(data: dict):
    os.makedirs(os.path.dirname(_FILE), exist_ok=True)
    with open(_FILE, "w") as f:
        json.dump(data, f)


def allocate(app_id: str, app_type: str) -> int:
    data = _load()
    used = set(data["allocated"].values())
    pool = _FRONTEND if app_type == "react" else _BACKEND
    for port in pool:
        if port not in used:
            data["allocated"][app_id] = port
            _save(data)
            return port
    raise RuntimeError(f"No available ports for '{app_type}'")


def release(app_id: str):
    data = _load()
    data["allocated"].pop(app_id, None)
    _save(data)


def get_all() -> dict:
    return _load().get("allocated", {})
