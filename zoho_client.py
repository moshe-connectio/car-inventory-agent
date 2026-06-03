import json
import os
import httpx
import logging

log = logging.getLogger(__name__)

TIMEOUT = 30

_ZAPIKEY = os.environ.get("ZOHO_APIKEY")
if not _ZAPIKEY:
    raise RuntimeError(
        "ZOHO_APIKEY לא מוגדר — הגדר אותו ב-.env / EnvironmentFile של השירות"
    )

_FN_BASE = "https://www.zohoapis.com/crm/v7/functions/{fn}/actions/execute"
_AUTH    = f"?auth_type=apikey&zapikey={_ZAPIKEY}"


def _fn_url(fn: str) -> str:
    return _FN_BASE.format(fn=fn) + _AUTH


_MANUFACTURER_URL = _fn_url("manufacturer_agent_do")

_MODEL_URL = _fn_url("car_model_agent_do")


def _call(url: str, body: dict) -> dict:
    resp = httpx.post(url, json=body, timeout=TIMEOUT)
    resp.raise_for_status()
    return json.loads(resp.content.decode("utf-8", errors="replace"))


# ── יצרנים ──────────────────────────────────────────────

def get_manufacturers() -> list[dict]:
    result = _call(_MANUFACTURER_URL, {"action": "get"})
    return result.get("message") or result.get("data") or []


def update_manufacturer(zoho_id: str, changes: dict) -> dict:
    return _call(_MANUFACTURER_URL, {"action": "update", "id": zoho_id, "data": changes})


# ── דגמים ────────────────────────────────────────────────

def get_models(manufacturer_id: str) -> list[dict]:
    result = _call(_MODEL_URL, {"action": "get", "manufacturer_id": manufacturer_id})
    return result.get("message") or result.get("data") or []


def create_model(data: dict) -> dict:
    return _call(_MODEL_URL, {"action": "create", "data": data})


def update_model(model_id: str, changes: dict) -> dict:
    return _call(_MODEL_URL, {"action": "update", "id": model_id, "data": changes})


# ── גרסאות (רמות גימור) ───────────────────────────────────

_TRIM_URL = _fn_url("carfinishlevelagentdo")


def get_trims(model_id: str) -> list[dict]:
    result = _call(_TRIM_URL, {"action": "get", "model_id": int(model_id)})
    data = result.get("message") or result.get("data") or []
    if not isinstance(data, list):
        import logging
        logging.getLogger(__name__).warning(f"[get_trims] תשובה לא צפויה: {data}")
        return []
    return data


def create_trim(data: dict) -> dict:
    return _call(_TRIM_URL, {"action": "create", "data": data})


def update_trim(trim_id: str, changes: dict) -> dict:
    return _call(_TRIM_URL, {"action": "update", "id": trim_id, "data": changes})
