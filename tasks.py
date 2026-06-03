import logging
import os
from celery import Celery

log = logging.getLogger(__name__)
REDIS = "redis://localhost:6379/0"

app = Celery("car-agent", broker=REDIS)
app.conf.update(
    result_backend=REDIS,
    worker_concurrency=4,
    task_acks_late=True,
    task_serializer="json",
    result_serializer="json",
    timezone="Asia/Jerusalem",
)

_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic")


def _chain_trim_scan(result: dict) -> None:
    """Queue a trim scan for EVERY active model of the manufacturer.

    After the model agent finishes, every model that is active in Zoho (i.e. passed
    validation and was created/kept active — excluding any just deactivated) gets its
    own trim-scan task. One task per model = a clear, serialized queue (the worker runs
    at concurrency 1), so trims are processed one model at a time with per-model retry,
    preventing overload and isolating failures.
    """
    if not isinstance(result, dict):
        return
    mfr_id  = str(result.get("mfr_id", "") or "")
    mfr_en  = result.get("manufacturer", "")
    mfr_he  = result.get("mfr_he", "")
    if not mfr_id:
        log.warning("[chain] אין mfr_id — לא ניתן לשרשר סריקת גרסאות")
        return

    # Models deactivated in this run must NOT get a trim scan.
    deactivated_ids = {
        str(c.get("id", "")) for c in result.get("changes", [])
        if c.get("action") == "deactivated" and c.get("id")
    }

    try:
        import zoho_client
        models = zoho_client.get_models(mfr_id)
    except Exception as e:
        log.error(f"[chain] get_models נכשל — לא שורשרו גרסאות: {e}")
        return

    dispatched = []
    for m in models:
        mid = str(m.get("id", "") or "")
        if not mid or mid in deactivated_ids:
            continue
        if not m.get("Active", True):
            continue
        payload = {
            "manufacturer": {"id": mfr_id, "name_en": mfr_en, "name_he": mfr_he},
            "models": [{
                "id":      mid,
                "name_en": m.get("Name", ""),
                "name_he": m.get("Car_Model_Name_HE", ""),
            }],
        }
        scan_trims_for_manufacturer.delay(payload)   # enqueued; worker processes serially
        dispatched.append(m.get("Name", ""))

    if dispatched:
        log.info(f"[chain] {len(dispatched)} סריקות גרסאות הוכנסו לתור עבור {mfr_en}: {dispatched}")
    else:
        log.info(f"[chain] {mfr_en}: אין דגמים פעילים לסריקת גרסאות")


@app.task(bind=True, max_retries=3, default_retry_delay=300)
def scan_models_for_manufacturer(self, models: list):
    try:
        if _PROVIDER == "openai":
            from agents.model_agent_openai import run
        else:
            from agents.model_agent import run
        result = run(models)
        _chain_trim_scan(result)
        return result
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task
def health_check():
    import redis as r
    r.Redis.from_url(REDIS).ping()
    return {"status": "ok", "provider": _PROVIDER}


@app.task(bind=True, max_retries=3, default_retry_delay=300)
def scan_trims_for_manufacturer(self, payload: dict):
    try:
        from agents.trim_agent_openai import run
        return run(payload)
    except Exception as exc:
        raise self.retry(exc=exc)
