"""
agents/trim_openai/fetcher.py
Single-call trim fetcher:
  One AI call per model — finds prices from icar.co.il (primary) or auto.co.il (fallback)
  and extracts a full 2026 spec sheet from icar/auto/gov.il in the same call.
  icar price is always authoritative when found; specs are read straight from the
  approved sources and never guessed.
"""
import logging

from openai import OpenAI

from ..model_openai.utils import ai_call, parse_json
from .fields import APPROVED_SOURCES, MAX_PRICE, MIN_PRICE, SPEC_KEYS

log = logging.getLogger(__name__)


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
    Single AI call: prices (icar primary, auto fallback) + full 2026 spec sheet
    (icar/auto for technical specs, gov.il for regulatory specs).
    Returns a list of trims with raw values (validation happens later in validator.py).
    """
    url_hint = _icar_url_hint(mfr_he, model_he)
    icar_line = (
        f"\n   Direct URL: {url_hint}"
        if url_hint
        else f"\n   Search: site:icar.co.il {search_name} מחירון"
    )

    prompt = f"""Research {search_name} trim levels, prices and FULL specs for Israel, model year 2026.

APPROVED SOURCES ONLY: icar.co.il | auto.co.il | gov.il (מינהל הרכב)
Every single data point — price AND every spec — must be read directly from one of
these three sites. Prefer the most current 2026 data shown.

══ STEP 1 — PRICES & SPECS from icar.co.il (PRIMARY SOURCE) ══{icar_line}
   • Open that URL and read the מחירון (price list) table — collect EVERY trim row
   • icar shows: trim name | engine | price (ILS) | and often monthly finance payment
   • Read the full specification table icar shows for each trim

══ STEP 2 — auto.co.il (FALLBACK — only if icar has NO price page) ══
   • Search https://www.auto.co.il/ for "{search_name}", open the model page
   • Collect all trims with MSRP prices in ILS and their spec comparison table

══ STEP 3 — gov.il (מינהל הרכב) — REGULATORY SPECS ══
   • For Israeli-market specs not on icar/auto, read gov.il:
     pollution level (דרגת זיהום אוויר 1-15), safety equipment level (רמת אבזור בטיחותי 0-8),
     official dimensions and curb weight.

══ COLLECT FOR EVERY TRIM (read from the page — never guess) ══
   price, monthly_payment, hp, torque_nm, engine_cc, transmission (e.g. אוטומטית 8 הילוכים),
   drivetrain (קדמית/אחורית/4X4), sec (0-100), top_speed, range_km, battery_kwh, charging_kw,
   fuel_consumption (km/L combined), pollution_level, safety_level,
   length_mm, width_mm, height_mm, trunk_liters, weight_kg, screen_inch, doors, seats,
   warranty (manufacturer warranty text, e.g. 3 שנים / 100,000 ק.מ).

══ STRICT RULES ══
✅ Prices must be ILS — Israeli new cars cost ₪{MIN_PRICE:,} to ₪{MAX_PRICE:,}
✅ source_url AND spec_source_url must be from icar.co.il, auto.co.il, or gov.il
✅ price_source: "icar" | "auto" | "gov"
✅ name_he must be UNIQUE per trim
✅ Electric trims: fill battery_kwh, charging_kw, range_km (no engine_cc/fuel_consumption)
✅ Combustion trims: fill engine_cc, fuel_consumption (no battery_kwh)
❌ NEVER invent or estimate any value — price or spec
❌ NEVER use ynet, walla, carexpert, manufacturer global sites, or any other source
❌ USD/EUR prices are wrong for Israel — do not include or convert
❌ If a spec field is unknown on the approved sources → set null, not a guess
❌ No double-quote characters inside Hebrew string values — use . instead (ק.מ not ק"מ)

If no current 2026 Israeli pricing found on approved sources → return {{"trims": []}}

Return ONLY valid JSON (no markdown):
{{
  "icar_found": true,
  "trims": [
    {{
      "name_he": "LT 2.0T AWD",
      "name_en": "LT 2.0T AWD",
      "price": 189900,
      "monthly_payment": 2490,
      "price_source": "icar",
      "source_url": "https://www.icar.co.il/...",
      "spec_source_url": "https://www.auto.co.il/...",
      "hp": 175,
      "torque_nm": 350,
      "engine_cc": 1998,
      "transmission": "אוטומטית 8 הילוכים",
      "drivetrain": "4X4",
      "sec": 8.5,
      "top_speed": 195,
      "range_km": null,
      "battery_kwh": null,
      "charging_kw": null,
      "fuel_consumption": 12.5,
      "pollution_level": 9,
      "safety_level": 7,
      "length_mm": 4630,
      "width_mm": 1875,
      "height_mm": 1675,
      "trunk_liters": 500,
      "weight_kg": 1720,
      "screen_inch": 10.2,
      "doors": 5,
      "seats": 5,
      "warranty": "3 שנים / 100,000 ק.מ"
    }}
  ]
}}"""

    text = ai_call(client, prompt)
    data = parse_json(text)
    raw  = data.get("trims", [])

    out = []
    seen = set()
    for t in raw:
        name  = (t.get("name_he") or "").strip()
        price = t.get("price")
        src   = (t.get("source_url") or "").lower()

        if not name or name.upper() in seen:
            continue
        if not isinstance(price, (int, float)) or not (MIN_PRICE <= int(price) <= MAX_PRICE):
            log.warning(f"  [fetch] '{name}' — מחיר לא תקין: {price!r}")
            continue
        if not any(s in src for s in APPROVED_SOURCES):
            log.warning(f"  [fetch] '{name}' — מקור מחיר לא מורשה: {src!r}")
            continue

        # Specs must also come from an approved page — otherwise drop ALL spec fields.
        spec_src = (t.get("spec_source_url") or "").lower()
        if spec_src and not any(s in spec_src for s in APPROVED_SOURCES):
            log.warning(f"  [fetch] '{name}' — מקור ספקים לא מורשה: {spec_src!r} — מנקה ספקים")
            for spec_field in SPEC_KEYS:
                t[spec_field] = None

        seen.add(name.upper())
        t["price"] = int(price)
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
    Single-call fetch: icar.co.il prices (primary) + auto/gov specs.
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
        if t.get("hp"):           parts.append(f"{t['hp']}כ.ס")
        if t.get("sec"):          parts.append(f"{t['sec']}שנ'")
        if t.get("range_km"):     parts.append(f"{t['range_km']}ק.מ")
        if t.get("battery_kwh"):  parts.append(f"{t['battery_kwh']}kWh")
        if t.get("transmission"): parts.append(str(t['transmission']))
        if t.get("drivetrain"):   parts.append(str(t['drivetrain']))
        if t.get("pollution_level"): parts.append(f"זיהום {t['pollution_level']}")
        if t.get("safety_level"):    parts.append(f"בטיחות {t['safety_level']}")
        mark = "📋" if src == "icar" else "🌐"
        log.info(f"    {mark} {t['name_he']} [{src}] | {' | '.join(parts)}")

    return trims
