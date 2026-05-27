"""
agents/trim_openai/fetcher.py
Single-call trim fetcher:
  One AI call per model — finds prices from icar.co.il (primary) or auto.co.il (fallback)
  and extracts full specs from auto.co.il in the same call.
  icar price is always authoritative when found; auto specs are always preferred.
"""
import logging

from openai import OpenAI

from ..model_openai.utils import ai_call, parse_json

log = logging.getLogger(__name__)

_MIN_PRICE = 55_000
_MAX_PRICE = 1_500_000
_APPROVED_SOURCES = ("icar.co.il", "auto.co.il", "gov.il")


def _icar_url_hint(mfr_he: str, model_he: str) -> str:
    """Construct the expected icar.co.il מחירון URL from Hebrew names."""
    if not mfr_he:
        return ""
    mfr_part = mfr_he.split()[0]
    if not model_he:
        return ""
    he_parts = model_he.split()
    if he_parts and he_parts[0] == mfr_part:
        he_parts = he_parts[1:]
    if not he_parts:
        return ""
    model_slug = "_".join(he_parts)
    return f"https://www.icar.co.il/{mfr_part}/{model_slug}/מחירון_רכב/"


def _fetch_all(
    client: OpenAI, search_name: str, mfr_he: str, model_he: str
) -> list[dict]:
    """
    Single AI call: find prices (icar primary, auto fallback) and full specs (auto).
    Returns validated trim list.
    """
    url_hint = _icar_url_hint(mfr_he, model_he)
    icar_line = (
        f"\n   Direct URL: {url_hint}"
        if url_hint
        else f"\n   Search: site:icar.co.il {search_name} מחירון"
    )

    prompt = f"""Research {search_name} trim levels and prices for Israel (2025-2026).

APPROVED SOURCES ONLY: icar.co.il | auto.co.il | gov.il (מינהל הרכב)
Every single data point — price AND spec — must be read directly from one of these three sites.

══ STEP 1 — PRICES from icar.co.il (PRIMARY SOURCE) ══{icar_line}
   • Open that URL and read the מחירון (price list) table
   • If the URL fails or shows no table, try: site:icar.co.il {search_name} מחירון
   • icar shows: trim name | engine | price (ILS)
   • Collect EVERY row — do not skip any trim

══ STEP 2 — PRICES from auto.co.il (FALLBACK — only if icar has NO price page) ══
   • Search https://www.auto.co.il/ for "{search_name}"
   • Find the model page with trim list and MSRP prices in ILS
   • Collect all trims with prices

══ STEP 3 — SPECS (for ALL trims found) ══
   • Fetch the auto.co.il trim comparison page for {search_name}
   • Extract specs AS SHOWN on the page: hp, 0-100 sec, range km, top speed, screen size, doors, seats
   • If a spec is not shown on the page → use null, never guess or estimate
   • spec_source_url must be the exact auto.co.il (or icar/gov.il) URL you read

══ STRICT RULES ══
✅ Prices must be ILS — Israeli new cars cost ₪{_MIN_PRICE:,} to ₪{_MAX_PRICE:,}
✅ source_url must be from icar.co.il, auto.co.il, or gov.il
✅ spec_source_url must be from icar.co.il, auto.co.il, or gov.il
✅ price_source: "icar" | "auto" | "gov"
✅ name_he must be UNIQUE per trim
❌ NEVER invent or estimate any value — price or spec
❌ NEVER use ynet, walla, carexpert, manufacturer sites, or any other source
❌ USD/EUR prices are wrong for Israel — do not include or convert
❌ Prices below ₪{_MIN_PRICE:,} are wrong — exclude them
❌ If a spec field is unknown → set null, not a guess

If no current 2025-2026 Israeli pricing found on approved sites → return {{"trims": []}}

Return ONLY valid JSON (no markdown):
{{
  "icar_found": true,
  "trims": [
    {{
      "name_he": "LT 2.0T AWD",
      "name_en": "LT 2.0T AWD",
      "price": 189900,
      "price_source": "icar",
      "source_url": "https://www.icar.co.il/...",
      "spec_source_url": "https://www.auto.co.il/...",
      "hp": 175,
      "sec": 8.5,
      "range_km": null,
      "top_speed": 195,
      "screen_inch": 10.2,
      "doors": 5,
      "seats": 5
    }}
  ]
}}"""

    text = ai_call(client, prompt)
    data = parse_json(text)
    raw   = data.get("trims", [])
    icar_found = bool(data.get("icar_found"))

    out = []
    seen = set()
    for t in raw:
        name  = (t.get("name_he") or "").strip()
        price = t.get("price")
        src   = (t.get("source_url") or "").lower()

        if not name or name.upper() in seen:
            continue
        if not isinstance(price, (int, float)) or not (_MIN_PRICE <= int(price) <= _MAX_PRICE):
            log.warning(f"  [fetch] '{name}' — מחיר לא תקין: {price!r}")
            continue
        if not any(s in src for s in _APPROVED_SOURCES):
            log.warning(f"  [fetch] '{name}' — מקור מחיר לא מורשה: {src!r}")
            continue

        spec_src = (t.get("spec_source_url") or "").lower()
        if spec_src and not any(s in spec_src for s in _APPROVED_SOURCES):
            log.warning(f"  [fetch] '{name}' — מקור ספקים לא מורשה: {spec_src!r} — מנקה ספקים")
            for spec_field in ("hp", "sec", "range_km", "top_speed", "screen_inch", "doors", "seats"):
                t[spec_field] = None

        seen.add(name.upper())
        t["price"]        = int(price)
        if "icar.co.il" in src:
            t["price_source"] = "icar"
        elif "gov.il" in src:
            t["price_source"] = "gov"
        else:
            t["price_source"] = "auto"
        out.append(t)

    icar_count = sum(1 for t in out if t.get("price_source") == "icar")
    auto_count = sum(1 for t in out if t.get("price_source") == "auto")
    log.info(
        f"  [fetch] {search_name}: {len(raw)} raw → {len(out)} תקינות "
        f"(icar:{icar_count} auto:{auto_count})"
    )
    return out


# ── Public entry point ─────────────────────────────────────────────────────────

def get_trims_ai(
    client: OpenAI, mfr_en: str, mfr_he: str, model_en: str, model_he: str
) -> list[dict]:
    """
    Single-call fetch: icar.co.il prices (primary) + auto.co.il specs.
    Returns [] if no Israeli prices found.
    """
    search_name = (
        model_en
        if model_en.upper().startswith(mfr_en.upper())
        else f"{mfr_en} {model_en}"
    )

    log.info(f"  [trim-ai] fetching: {search_name}")
    trims = _fetch_all(client, search_name, mfr_he, model_he)

    if not trims:
        log.info(f"  [trim-ai] {search_name}: לא נמצאו מחירים — מדלג")
        return []

    log.info(f"  [trim-ai] {search_name}: {len(trims)} גרסאות סופיות")
    for t in trims:
        parts = [f"₪{t['price']:,}"]
        src   = t.get("price_source", "?")
        if t.get("hp"):          parts.append(f"{t['hp']}כ\"ס")
        if t.get("sec"):         parts.append(f"{t['sec']}שנ'")
        if t.get("range_km"):    parts.append(f"{t['range_km']}ק\"מ")
        if t.get("top_speed"):   parts.append(f"{t['top_speed']}קמ\"ש")
        if t.get("screen_inch"): parts.append(f"{t['screen_inch']}\"")
        if t.get("doors"):       parts.append(f"{t['doors']}ד'")
        if t.get("seats"):       parts.append(f"{t['seats']}מ'")
        mark = "📋" if src == "icar" else "🌐"
        log.info(f"    {mark} {t['name_he']} [{src}] | {' | '.join(parts)}")

    return trims
