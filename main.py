import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)
app = FastAPI(title="Car Agent — Models")


def _raw_decode(raw: str):
    """Parses the first valid JSON object from a string, ignores trailing data."""
    decoder = json.JSONDecoder()
    return decoder.raw_decode(raw.strip())[0]


async def _parse_payload(request: Request):
    body = await request.body()
    raw  = body.decode("utf-8", errors="replace")
    ct   = request.headers.get("content-type", "")
    log.info(f"[webhook] ct={ct!r} | {len(raw)} chars | {raw[:200]}")
    return _raw_decode(raw)


def _build_from_mfr_record(mfr_rec: dict) -> dict:
    """
    Deluge שולח רשומת יצרן בלבד — שולפים דגמים קשורים ובונים payload מלא.
    """
    import sys, os
    sys.path.insert(0, "/opt/car-agent")
    from zoho_client import _call, _MODEL_URL

    mfr_id      = str(mfr_rec.get("id", ""))
    mfr_name_en = mfr_rec.get("Car_Manufacturer_Name_EN") or mfr_rec.get("Name", "")
    mfr_name_he = mfr_rec.get("Name") if mfr_rec.get("Car_Manufacturer_Name_EN") else ""

    log.info(f"[webhook] יצרן מזוהה: {mfr_name_en} (id={mfr_id})")

    # שלוף דגמים קשורים דרך הפונקציה
    result  = _call(_MODEL_URL, {"action": "get", "manufacturer_id": int(mfr_id)})
    models  = result.get("message") if isinstance(result.get("message"), list) else []

    if not models:
        # נסה דרך success→message ישיר
        inner = result.get("message") or result.get("data") or []
        models = inner if isinstance(inner, list) else []

    log.info(f"[webhook] דגמים שנשלפו: {len(models)}")

    return {
        "manufacturer": {
            "id":      mfr_id,
            "name":    mfr_name_en,
            "name_he": mfr_name_he,
        },
        "models": models,
    }


@app.post("/webhook/models")
async def models_webhook(request: Request):
    try:
        payload = await _parse_payload(request)
    except Exception as e:
        log.error(f"[webhook] parse error: {e}")
        return JSONResponse(status_code=400, content={"error": str(e)})

    # זיהוי: האם קיבלנו רשומת יצרן (מ-Deluge) או payload מובנה?
    is_mfr_record = (
        isinstance(payload, dict)
        and ("Car_Manufacturer_Name_EN" in payload or
             ("Name" in payload and "models" not in payload and "manufacturer" not in payload))
    )

    if is_mfr_record:
        log.info("[webhook] זוהתה רשומת יצרן — שולף דגמים")
        try:
            payload = _build_from_mfr_record(payload)
        except Exception as e:
            log.error(f"[webhook] _build_from_mfr_record error: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

    # שלוף פרטים לתצוגה
    if isinstance(payload, dict):
        mfr    = payload.get("manufacturer") or {}
        mfr_en = mfr.get("name_en") or mfr.get("name") or ""
        models = payload.get("models", [])
    else:
        models  = payload if isinstance(payload, list) else []
        mfr_obj = (models[0].get("Manufacturer") or {}) if models else {}
        mfr_en  = mfr_obj.get("name") or ""

    from tasks import scan_models_for_manufacturer
    task = scan_models_for_manufacturer.delay(payload)

    return {
        "status":       "queued",
        "task_id":      task.id,
        "manufacturer": mfr_en,
        "models_count": len(models),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status/{task_id}")
async def task_status(task_id: str):
    from tasks import app as celery_app
    result = celery_app.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "status":  result.status,
        "result":  result.result if result.ready() else None,
    }


# ── Trims Webhook ─────────────────────────────────────────────────────────────

def _build_trims_payload(mfr_rec: dict) -> dict:
    """
    מקבל רשומת יצרן מ-Deluge → שולף דגמים מ-Zoho → בונה payload לסוכן הגרסאות.
    """
    import sys, os
    sys.path.insert(0, "/opt/car-agent")
    from zoho_client import _call, _MODEL_URL

    mfr_id      = str(mfr_rec.get("id", ""))
    mfr_name_en = mfr_rec.get("Car_Manufacturer_Name_EN") or mfr_rec.get("Name", "")
    mfr_name_he = mfr_rec.get("Name") if mfr_rec.get("Car_Manufacturer_Name_EN") else ""

    log.info(f"[webhook/trims] יצרן: {mfr_name_en} (id={mfr_id})")

    result = _call(_MODEL_URL, {"action": "get", "manufacturer_id": int(mfr_id)})
    models = result.get("message") if isinstance(result.get("message"), list) else []
    if not models:
        models = result.get("data") or []

    slim_models = [
        {
            "id":      str(m.get("id", "")),
            "name_en": m.get("Name", ""),
            "name_he": m.get("Car_Model_Name_HE", ""),
        }
        for m in models if m.get("id") and m.get("Active", True)
    ]

    log.info(f"[webhook/trims] {len(slim_models)} דגמים פעילים")

    return {
        "manufacturer": {
            "id":      mfr_id,
            "name_en": mfr_name_en,
            "name_he": mfr_name_he,
        },
        "models": slim_models,
    }


@app.post("/webhook/trims")
async def trims_webhook(request: Request):
    try:
        payload = await _parse_payload(request)
    except Exception as e:
        log.error(f"[webhook/trims] parse error: {e}")
        return JSONResponse(status_code=400, content={"error": str(e)})

    # זיהוי: רשומת יצרן (מ-Deluge) או payload מובנה?
    is_mfr_record = (
        isinstance(payload, dict)
        and ("Car_Manufacturer_Name_EN" in payload or
             ("Name" in payload and "models" not in payload and "manufacturer" not in payload))
    )

    if is_mfr_record:
        log.info("[webhook/trims] זוהתה רשומת יצרן — שולף דגמים")
        try:
            payload = _build_trims_payload(payload)
        except Exception as e:
            log.error(f"[webhook/trims] שגיאה בבניית payload: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

    mfr    = (payload.get("manufacturer") or {}) if isinstance(payload, dict) else {}
    mfr_en = mfr.get("name_en") or mfr.get("name") or ""
    models = payload.get("models", []) if isinstance(payload, dict) else []

    from tasks import scan_trims_for_manufacturer
    task = scan_trims_for_manufacturer.delay(payload)

    return {
        "status":       "queued",
        "task_id":      task.id,
        "manufacturer": mfr_en,
        "models_count": len(models),
    }
