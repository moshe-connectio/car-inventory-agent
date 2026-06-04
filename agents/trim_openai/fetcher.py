"""
agents/trim_openai/fetcher.py
icar-only trim fetcher — deterministic, ZERO invented data.

Every price and spec comes straight from icar.co.il's internal JSON API
(agents/scrapers/icar_api.py). auto.co.il is Cloudflare-blocked and cannot be read
deterministically, so the previous LLM web-search path was removed entirely: if a model
is not found on icar we return nothing rather than risk inventing prices/specs.

The only AI use left is naming.py, which transliterates icar's OWN grade names to Hebrew —
it never fabricates any data value.
"""
import logging

from openai import OpenAI

log = logging.getLogger(__name__)


def get_trims_ai(
    client: OpenAI, mfr_en: str, mfr_he: str, model_en: str, model_he: str
) -> list[dict]:
    """
    Return the complete current trim list for a model — exclusively from icar's API.
    Returns [] if the model is not on icar (we never invent data).
    """
    search_name = (
        model_en
        if model_en.upper().startswith(mfr_en.upper())
        else f"{mfr_en} {model_en}"
    )
    log.info(f"  [trim-ai] fetching: {search_name}")

    from ..scrapers import icar_api
    trims = icar_api.get_trims(mfr_en, mfr_he, model_en, model_he)

    if not trims:
        log.info(f"  [trim-ai] {search_name}: לא נמצא ב-icar — מדלג (ללא המצאת נתונים)")
        return []

    # clean, professional, bilingual names (transliterates icar's grade — no data invented)
    from .naming import clean_trim_names
    clean_trim_names(client, mfr_en, model_en, trims)

    log.info(f"  [trim-ai] {search_name}: {len(trims)} גרסאות סופיות [icar]")
    for t in trims:
        parts = [f"₪{t['price']:,}"]
        if t.get("hp"):           parts.append(f"{int(t['hp'])}כ.ס")
        if t.get("sec"):          parts.append(f"{t['sec']}שנ'")
        if t.get("range_km"):     parts.append(f"{int(t['range_km'])}ק.מ")
        if t.get("battery_kwh"):  parts.append(f"{t['battery_kwh']}kWh")
        if t.get("transmission"): parts.append(str(t["transmission"]))
        if t.get("drivetrain"):   parts.append(str(t["drivetrain"]))
        log.info(f"    📋 {t['name_he']} / {t['name_en']} | {' | '.join(parts)}")

    return trims
