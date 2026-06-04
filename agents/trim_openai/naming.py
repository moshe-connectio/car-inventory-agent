"""
agents/trim_openai/naming.py
Clean, professional, bilingual trim-level names.

icar gives raw strings like "1.2 טורבו-בנזין 1RS" or "1.6 טורבו-בנזין היברידי Long Executive".
We want just the trim/grade — "1RS", "Long Executive", "GTB" — in BOTH Hebrew and English,
dropping engine size, fuel type, transmission and seat counts. When two trims of the same
model share a grade, the engine is appended to disambiguate (e.g. "Premium 1.2L").

One AI text-normalisation call per model (cheap, no web search). Falls back to a
deterministic strip if the AI call fails.
"""
import logging
import re

from openai import OpenAI

from ..model_openai.utils import ai_call, parse_json

log = logging.getLogger(__name__)

# Hebrew tokens to strip in the deterministic fallback (fuel / transmission / drivetrain).
_NOISE = re.compile(
    r"טורבו|בנזין|דיזל|היבריד[ית]?|נטען|חשמלי|מגדש|אוטומט(ית)?|ידנית|רובוטית|"
    r"טיפטרוניק|רציפה|מקומות|מושבים|הנעה|כפולה|דאבל",
    re.I,
)


_FUEL_PATS = [
    ("PHEV",   r"נטען|plug"),
    ("EV",     r"חשמלי|\bEV\b"),
    ("Hybrid", r"היבריד|hybrid"),
    ("Diesel", r"דיזל|diesel"),
    ("Petrol", r"בנזין|petrol|טורבו"),
]


def _fuel(t: dict) -> str:
    raw = t.get("_raw") or ""
    for label, pat in _FUEL_PATS:          # raw name is authoritative (נטען=PHEV, היברידי=Hybrid)
        if re.search(pat, raw, re.I):
            return label
    if t.get("battery_kwh") and not t.get("engine_cc"):
        return "EV"
    if t.get("battery_kwh") and t.get("engine_cc"):
        return "Hybrid"
    return ""


def _liters(t: dict) -> str:
    cc = t.get("engine_cc")
    if not cc:
        return ""
    try:
        return f"{float(cc) / 1000:.1f}".rstrip("0").rstrip(".") + "L"
    except (TypeError, ValueError):
        return ""


def _dt_en(t: dict) -> str:
    return {"4X4": "AWD", "קדמית": "FWD", "אחורית": "RWD"}.get(t.get("drivetrain"), "")


def _strip_grade(raw: str) -> str:
    """Deterministic fallback: drop displacement + noise words, keep the rest."""
    s = re.sub(r"\d+\.\d+", " ", raw or "")      # engine displacement 1.2 / 3.0
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[-־]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,-")
    return s


def _disambiguate(trims: list[dict]) -> None:
    """Append only the attribute(s) that actually differ to trims sharing a grade."""
    groups: dict[str, list[dict]] = {}
    for t in trims:
        groups.setdefault((t.get("name_en") or "").strip().upper(), []).append(t)

    for group in groups.values():
        if len(group) < 2:
            continue
        # use, in priority order, only the attributes that vary within the group
        funcs = [f for f in (_fuel, _liters, _dt_en)
                 if len({f(t) for t in group}) > 1]
        for t in group:
            extra = " ".join(filter(None, (f(t) for f in funcs)))
            if extra:
                t["name_en"] = f"{t['name_en']} {extra}".strip()
                t["name_he"] = f"{t['name_he']} {extra}".strip()
        # final guarantee of uniqueness
        seen: dict[str, int] = {}
        for t in group:
            key = t["name_en"].upper()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                t["name_en"] += f" {seen[key]}"
                t["name_he"] += f" {seen[key]}"


def clean_trim_names(client: OpenAI, mfr_en: str, model_en: str, trims: list[dict]) -> None:
    """Fill clean professional name_en + name_he (grade only) for every trim, in place."""
    if not trims:
        return

    raws = [(t.get("name_he") or t.get("name_en") or "").strip() for t in trims]
    listing = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(raws))
    prompt = f"""These are raw trim/version strings for the {mfr_en} {model_en} from an Israeli car price list.
For EACH, output the clean professional TRIM-LEVEL (finish grade) name only.

RULES:
- Keep ONLY the grade/trim designation: e.g. "1RS", "Long Executive", "GTB", "Premium", "GT-Line".
- DROP engine displacement (1.2, 3.0), fuel type (בנזין/טורבו/היברידי/חשמלי/דיזל/נטען),
  transmission (אוטומט/ידנית/רובוטית), and seat counts.
- name_en = grade in English (Latin). name_he = same grade (grades stay in Latin if that's how they're branded).
- If a version has NO distinct grade (only engine info), use "Standard".
- Return EXACTLY {len(raws)} items, in the SAME order.

Raw strings:
{listing}

Return ONLY JSON:
{{"trims": [{{"name_en": "...", "name_he": "..."}}]}}"""

    out = []
    try:
        out = parse_json(ai_call(client, prompt)).get("trims", [])
    except Exception as e:
        log.warning(f"  [naming] נורמליזציה נכשלה ({model_en}): {e}")

    for i, t in enumerate(trims):
        item = out[i] if i < len(out) and isinstance(out[i], dict) else {}
        en = (item.get("name_en") or "").strip()
        he = (item.get("name_he") or "").strip()
        if not en and not he:                      # AI failed for this row → deterministic
            en = he = _strip_grade(raws[i]) or "Standard"
        t["name_en"] = en or he
        t["name_he"] = he or en
        t["_raw"] = raws[i]                         # used by disambiguation (fuel detection)

    _disambiguate(trims)
    for t in trims:
        t.pop("_raw", None)
    log.info(f"  [naming] {model_en}: " + ", ".join(t["name_en"] for t in trims))
