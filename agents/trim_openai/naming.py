"""
agents/trim_openai/naming.py
Clean, professional, bilingual trim-level names — accurate, no invented grades.

The English grade is extracted DETERMINISTICALLY from icar's raw version string (the Latin
tokens, dropping engine displacement, fuel/battery words, drivetrain codes and Hebrew spec
text). This is the source of truth — the AI never reinterprets it (icar "2.0 300 כ\"ס VZ"
→ grade "VZ", never "GT").

The AI is used ONLY to transliterate that exact grade to Hebrew for the Name field
(Premium→פרימיום); short codes/acronyms (VZ, GT, 1RS, EX, GTB) stay Latin. When two trims
of a model share a grade, the differing attribute (fuel → engine → drivetrain) is appended,
in Hebrew on name_he and English on name_en.
"""
import logging
import re

from openai import OpenAI

from ..model_openai.utils import ai_call, parse_json

log = logging.getLogger(__name__)

_DRIVETRAIN_CODE = re.compile(r"^[24][xX×][24]$")        # 2x4 / 4x4 / 2X4
_FUEL_PATS = [
    ("PHEV",   r"נטען|plug"),
    ("EV",     r"חשמלי|\bEV\b"),
    ("Hybrid", r"היבריד|hybrid|מיקרו"),
    ("Diesel", r"דיזל|diesel"),
    ("Petrol", r"בנזין|petrol|טורבו"),
]
_FUEL_HE = {"PHEV": "היברידי נטען", "EV": "חשמלי", "Hybrid": "היברידי",
            "Diesel": "דיזל", "Petrol": "בנזין"}
_DT_EN = {"4X4": "AWD", "קדמית": "FWD", "אחורית": "RWD"}


# ── deterministic grade extraction from icar's raw version_name ──────────────────

def _extract_grade(raw: str) -> str:
    """
    Keep only the Latin grade tokens from an icar version string.
    Drops displacement (2.0), counts (300), Hebrew spec words (טורבו-בנזין, כ"ס, סוללת),
    and drivetrain codes (2X4/4X4). e.g. "2.0 טורבו-בנזין 333 כ\"ס VZ 4X4" → "VZ".
    """
    out = []
    for tok in (raw or "").split():
        if re.fullmatch(r"\d+(\.\d+)?", tok):        # pure number / displacement
            continue
        if _DRIVETRAIN_CODE.match(tok):              # 2X4 / 4X4
            continue
        if re.search(r"[א-ת]", tok):                 # any Hebrew → spec text, not grade
            continue
        if re.search(r"[A-Za-z]", tok):              # a Latin grade token (VZ, 1RS, Premium…)
            out.append(tok.strip("-"))
    return " ".join(out).strip()


def _sanitize(s: str) -> str:
    """Strip JSON/quote artifacts that can leak from AI output."""
    s = re.sub(r'["“”{}\[\]\\]', "", s or "")
    s = re.sub(r"\s+", " ", s).strip(" -,'")
    return s


# ── disambiguation helpers ──────────────────────────────────────────────────────

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


def _tag_fuel(t):   f = _fuel(t);   return (f, _FUEL_HE.get(f, f))
def _tag_liters(t): l = _liters(t); return (l, l)
def _tag_dt(t):     d = t.get("drivetrain") or ""; return (_DT_EN.get(d, ""), d)


def _disambiguate(trims: list[dict]) -> None:
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


# ── Hebrew transliteration (AI, formatting only — never changes the grade) ───────

def _transliterate(client: OpenAI, grades: list[str]) -> dict[str, str]:
    uniq = sorted({g for g in grades if g})
    if not uniq:
        return {}
    listing = "\n".join(f"- {g}" for g in uniq)
    prompt = f"""Transliterate these car trim-GRADE names to Hebrew letters.

- Pronounceable words → Hebrew letters: Premium→פרימיום, Comfort→קומפורט, Pro→פרו,
  Long→לונג, Executive→אקזקיוטיב, Performance→פרפורמנס, Design→דיזיין, Urban→אורבן,
  Ultimate→אולטימייט, Excellence→אקסלנס, Pure→פיור, Standard→סטנדרט.
- Short codes / acronyms with no Hebrew form → KEEP IN LATIN exactly: VZ, GT, GTB, 1RS,
  2RS, EX, ST, N-Line, GT-Line.
- Do NOT use double-quote characters in any value.

Grades:
{listing}

Return ONLY JSON mapping each input grade to its Hebrew form:
{{"map": {{"Premium": "פרימיום", "VZ": "VZ"}}}}"""
    try:
        return parse_json(ai_call(client, prompt)).get("map", {}) or {}
    except Exception as e:
        log.warning(f"  [naming] תעתוק נכשל: {e}")
        return {}


def clean_trim_names(client: OpenAI, mfr_en: str, model_en: str, trims: list[dict]) -> None:
    """Set deterministic English grade + Hebrew transliteration for each trim, in place."""
    if not trims:
        return

    raws   = [(t.get("name_he") or t.get("name_en") or "").strip() for t in trims]
    grades = [_sanitize(_extract_grade(r)) or "Standard" for r in raws]
    he_map = _transliterate(client, grades)

    for i, t in enumerate(trims):
        g = grades[i]
        t["name_en"] = g
        t["name_he"] = _sanitize(he_map.get(g, "")) or g     # Hebrew translit, else Latin grade
        t["_raw"]    = raws[i]

    _disambiguate(trims)
    for t in trims:
        t.pop("_raw", None)
    log.info(f"  [naming] {model_en}: " +
             ", ".join(f"{t['name_he']} / {t['name_en']}" for t in trims))
