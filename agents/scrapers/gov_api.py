"""
agents/scrapers/gov_api.py
Fallback trim source via data.gov.il — the Ministry of Transport vehicle registry.

Used ONLY when a manufacturer is entirely absent from icar.co.il (see fetcher). The
registry (resource 053cea08-…) lists the real trim levels (`ramat_gimur`) actually
registered on Israeli roads, plus fuel type and safety level — but NO price (the gov
data has none; the validator allows gov.il-sourced trims without a price). The registry
is per-vehicle and noisy, so we keep only current model-years and drop long-tail /
typo trims below a minimum registration count.

Public entry point: get_trims(mfr_en, mfr_he, model_en, model_he) → runner-compatible dicts.
"""
import collections
import datetime
import json
import logging

import httpx

log = logging.getLogger(__name__)

_RID     = "053cea08-09bc-40ec-8f7a-156f0677aff3"
_URL     = "https://data.gov.il/api/3/action/datastore_search"
_H       = {"User-Agent": "Mozilla/5.0 (compatible; CarAgentBot/1.0)"}
_TIMEOUT = 60
_CUR_YEAR = datetime.date.today().year
_MIN_YEAR = _CUR_YEAR - 1          # "current" = this year or last year
# Drop long-tail/typo trims relative to the model's volume: filters noise for high-volume
# models yet keeps real trims of niche (absent-from-icar) brands with few registrations.
_MIN_FLOOR = 3
_MIN_FRACTION = 0.005


def _search(filters: dict, limit: int = 1000, offset: int = 0) -> dict:
    r = httpx.get(_URL, headers=_H, timeout=_TIMEOUT, params={
        "resource_id": _RID,
        "filters": json.dumps(filters, ensure_ascii=False),
        "limit": limit, "offset": offset,
    })
    r.raise_for_status()
    return r.json().get("result", {})


def _fetch_model_year(model_upper: str, year: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        res   = _search({"kinuy_mishari": model_upper, "shnat_yitzur": year}, 1000, offset)
        batch = res.get("records", [])
        out  += batch
        if len(batch) < 1000 or len(out) >= res.get("total", 0):
            break
        offset += 1000
    return out


def get_trims(mfr_en: str, mfr_he: str, model_en: str, model_he: str) -> list[dict]:
    """Real registered trim levels for a model from data.gov.il (no price). [] on miss."""
    model_upper = (model_en or "").strip().upper()
    if not model_upper:
        return []

    try:
        rows: list[dict] = []
        for y in range(_MIN_YEAR, _CUR_YEAR + 1):
            rows += _fetch_model_year(model_upper, y)
    except Exception as e:
        log.warning(f"[gov-api] נכשל עבור {mfr_en} {model_en}: {e}")
        return []

    # keep only rows of THIS manufacturer (tozeret_nm is like 'קיה סלובקיה' / 'יונדאי טורקיה')
    mhe = (mfr_he or "").strip()
    men = (mfr_en or "").strip().upper()

    def _mfr_ok(tz: str) -> bool:
        tz = tz or ""
        return bool((mhe and mhe in tz) or (men and men in tz.upper()))

    agg: dict[str, dict] = collections.defaultdict(
        lambda: {"n": 0, "safety": collections.Counter(), "fuel": collections.Counter()})
    for r in rows:
        if not _mfr_ok(r.get("tozeret_nm")):
            continue
        g = (r.get("ramat_gimur") or "").strip()
        if not g:
            continue
        a = agg[g]
        a["n"] += 1
        s = r.get("ramat_eivzur_betihuty")
        if s not in (None, ""):
            a["safety"][str(s)] += 1
        f = (r.get("sug_delek_nm") or "").strip()
        if f:
            a["fuel"][f] += 1

    total = sum(a["n"] for a in agg.values())
    min_count = max(_MIN_FLOOR, int(total * _MIN_FRACTION))
    trims: list[dict] = []
    for g, a in agg.items():
        if a["n"] < min_count:                          # long-tail / typo noise
            continue
        trim = {
            "name_he":         g,
            "name_en":         "",
            "price":           None,                    # gov has no price (allowed by validator)
            "price_source":    "gov.il",
            "source_url":      "https://data.gov.il",
            "spec_source_url": "https://data.gov.il",
        }
        saf = a["safety"].most_common(1)[0][0] if a["safety"] else None
        if saf is not None and str(saf).isdigit():
            trim["safety_level"] = int(saf)
        trims.append(trim)

    log.info(f"[gov-api] {mfr_en} {model_en}: {len(trims)} גרסאות מ-gov.il "
             f"(ללא מחיר, סף {min_count} רישומים)")
    return trims
