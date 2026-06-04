"""
agents/trim_openai/naming.py
Clean, professional, bilingual trim-level names.

icar gives raw strings like "סוללת 86 קוט\"ש Comfort" or "1.6 טורבו-בנזין Long Executive".
We want just the trim/grade, dropping engine size, fuel type, transmission and seat counts.

  • name_he (the Hebrew Name field): the grade transliterated to Hebrew when it's a
    pronounceable word (Comfort→קומפורט, Premium→פרימיום). Short alphanumeric codes /
    acronyms with no Hebrew form (GT, GTB, 1RS, EX, N-Line) stay in Latin.
  • name_en: the grade in English/Latin (always filled).

When two trims of the same model share a grade, the differing attribute (fuel → engine →
drivetrain) is appended — in Hebrew on name_he and in English on name_en.

One AI text-normalisation call per model (cheap, no web search). Falls back to a
deterministic strip if the AI call fails.
"""
import logging
import re

from openai import OpenAI

from ..model_openai.utils import ai_call, parse_json

log = logging.getLogger(__name__)

_NOISE = re.compile(
    r"טורבו|בנזין|דיזל|היבריד[ית]?|נטען|חשמלי|מגדש|אוטומט(ית)?|ידנית|רובוטית|"
    r"טיפטרוניק|רציפה|מקומות|מושבים|הנעה|כפולה|דאבל|אחורית|קדמית|סוללת|קוט\"ש|ליטר",
    re.I,
)

_FUEL_PATS = [
    ("PHEV",   r"נטען|plug"),
    ("EV",     r"חשמלי|\bEV\b"),
    ("Hybrid", r"היבריד|hybrid"),
    ("Diesel", r"דיזל|diesel"),
    ("Petrol", r"בנזין|petrol|טורבו"),
]
_FUEL_HE = {"PHEV": "היברידי נטען", "EV": "חשמלי", "Hybrid": "היברידי",
            "Diesel": "דיזל", "Petrol": "בנזין"}
_DT_EN   = {"4X4": "AWD", "קדמית": "FWD", "אחורית": "RWD"}


def _fuel(t: dict) -> str:
    raw = t.get("_raw") or ""
    for label, pat in _FUEL_PATS:
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


# attribute → (english tag, hebrew tag)
def _tag_fuel(t):    f = _fuel(t);     return (f, _FUEL_HE.get(f, f))
def _tag_liters(t):  l = _liters(t);   return (l, l)
def _tag_dt(t):      d = t.get("drivetrain") or ""; return (_DT_EN.get(d, ""), d)


def _strip_grade(raw: str) -> str:
    """Deterministic fallback: drop displacement + noise words, keep the rest."""
    s = re.sub(r"\d+\.\d+", " ", raw or "")
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[-־]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,-")
    return s


def _disambiguate(trims: list[dict]) -> None:
    """Append only the attribute(s) that differ — Hebrew to name_he, English to name_en."""
    groups: dict[str, list[dict]] = {}
    for t in trims:
        groups.setdefault((t.get("name_en") or "").strip().upper(), []).append(t)

    for group in groups.values():
        if len(group) < 2:
            continue
        taggers = [fn for fn in (_tag_fuel, _tag_liters, _tag_dt)
                   if len({fn(t)[0] for t in group}) > 1]
        for t in group:
            en_extra = " ".join(filter(None, (fn(t)[0] for fn in taggers)))
            he_extra = " ".join(filter(None, (fn(t)[1] for fn in taggers)))
            if en_extra:
                t["name_en"] = f"{t['name_en']} {en_extra}".strip()
            if he_extra:
                t["name_he"] = f"{t['name_he']} {he_extra}".strip()
        seen: dict[str, int] = {}
        for t in group:
            key = t["name_en"].upper()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                t["name_en"] += f" {seen[key]}"
                t["name_he"] += f" {seen[key]}"


def clean_trim_names(client: OpenAI, mfr_en: str, model_en: str, trims: list[dict]) -> None:
    """Fill professional name_en (English) + name_he (Hebrew grade) for every trim, in place."""
    if not trims:
        return

    raws = [(t.get("name_he") or t.get("name_en") or "").strip() for t in trims]
    listing = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(raws))
    prompt = f"""These are raw trim/version strings for the {mfr_en} {model_en} from an Israeli car price list.
For EACH, output the clean professional TRIM-LEVEL (finish grade) name only.

Keep ONLY the grade designation. DROP engine displacement (1.2, 3.0), battery info
(סוללת.. קוט"ש), fuel type (בנזין/טורבו/היברידי/חשמלי/דיזל/נטען), transmission, drivetrain
(הנעה/כפולה), and seat counts.

For each grade return two forms:
- "name_en": the grade in English/Latin (e.g. "Comfort", "Premium", "Long Executive", "GT", "1RS").
- "name_he": the grade in HEBREW. Transliterate pronounceable grade words to Hebrew letters
  (Comfort→קומפורט, Premium→פרימיום, Pro→פרו, Long→לונג, Executive→אקזקיוטיב, Performance→פרפורמנס,
  Design→דיזיין, Ultimate→אולטימייט, Urban→אורבן, Standard→סטנדרט).
  BUT keep short alphanumeric codes/acronyms in Latin — they have no Hebrew form
  (GT, GTB, RS, 1RS, 2RS, EX, ST, N-Line, GT-Line).
- If a version has NO distinct grade (only engine/battery info), use name_en="Standard", name_he="סטנדרט".
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
    log.info(f"  [naming] {model_en}: " +
             ", ".join(f"{t['name_he']} / {t['name_en']}" for t in trims))
