import json
import httpx
import logging

log = logging.getLogger(__name__)

TIMEOUT = 30

_MANUFACTURER_URL = (
    "https://www.zohoapis.com/crm/v7/functions/manufacturer_agent_do/actions/execute"
    "?auth_type=apikey&zapikey=1003.0e96001b632e81eb3d8f9880e9c49739.97e21cbe9c4d8a57b4be4be3350ab4ab"
)

_MODEL_URL = (
    "https://www.zohoapis.com/crm/v7/functions/car_model_agent_do/actions/execute"
    "?auth_type=apikey&zapikey=1003.0e96001b632e81eb3d8f9880e9c49739.97e21cbe9c4d8a57b4be4be3350ab4ab"
)


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

_TRIM_URL = (
    "https://www.zohoapis.com/crm/v7/functions/carfinishlevelagentdo/actions/execute"
    "?auth_type=apikey&zapikey=1003.0e96001b632e81eb3d8f9880e9c49739.97e21cbe9c4d8a57b4be4be3350ab4ab"
)


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
