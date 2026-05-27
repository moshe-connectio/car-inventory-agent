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
    """Dispatch a trim scan task for each model that was created or activated."""
    if not isinstance(result, dict):
        return
    changes = result.get("changes", [])
    mfr_id  = result.get("mfr_id", "")
    mfr_en  = result.get("manufacturer", "")
    mfr_he  = result.get("mfr_he", "")

    dispatched = []
    for change in changes:
        if change.get("action") not in ("created", "activated"):
            continue
        model_id = change.get("id") or change.get("zoho_id")
        if not model_id:
            log.warning(f"[chain] trim scan דולג — אין ID לדגם: {change.get('name', '')}")
            continue
        payload = {
            "manufacturer": {"id": mfr_id, "name_en": mfr_en, "name_he": mfr_he},
            "models": [{
                "id":      model_id,
                "name_en": change.get("name", ""),
                "name_he": change.get("name_he", ""),
            }],
        }
        scan_trims_for_manufacturer.delay(payload)
        dispatched.append(change.get("name", ""))
        log.info(f"[chain] trim scan → {mfr_en} / {change.get('name','')} [{change.get('action')}]")

    if dispatched:
        log.info(f"[chain] {len(dispatched)} trim scans הועברו: {dispatched}")


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
