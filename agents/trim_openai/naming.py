"""
agents/trim_openai/naming.py
Clean, professional, bilingual trim-level names — accurate, DETERMINISTIC, no invented data.

Both names come straight from icar's raw version string (e.g. "1.6 טורבו-בנזין Premium"),
with NO AI involved:

  • name_en (Car_Finish_level_Name_EN) — the Latin grade tokens only (Premium, M-Sport, RS,
    M240i xDrive…); engine displacement, fuel/spec words, units and drivetrain codes dropped.
  • name_he (Name) — the same grade PLUS any meaningful Hebrew descriptor icar provides
    (חשמלי, הנעה כפולה, and rare Hebrew grade names like פרימיום/סטינגריי). Hebrew *spec*
    words (טורבו-בנזין, כ"ס, ספורטבק, טון…) are dropped via _HE_SPEC_DROP.

icar does NOT supply Hebrew grade names — grades are Latin — so per the product decision the
Hebrew name keeps real words in English; only genuine Hebrew from icar survives. When two trims
of a model share a grade, the differing attribute (fuel → engine → drivetrain) is appended,
in Hebrew on name_he and English on name_en.
"""
import logging
import re

log = logging.getLogger(__name__)

_DRIVETRAIN_CODE = re.compile(r"^[24][xX×][24]$")        # 2x4 / 4x4 / 2X4
_NUM = re.compile(r"^\d+([.,]\d+)?$")                     # 1.6 / 400 / 116 / 4,117
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

# Hebrew SPEC words to drop from names (engine/fuel/transmission/body/dimension). Everything
# else Hebrew is a meaningful descriptor and is KEPT (חשמלי, הנעה, כפולה, אחורית, קדמית,
# פרימיום, סטינגריי, טק…). Derived from a scan of all ~2036 current icar version names.
_HE_SPEC_DROP = {
    # דלק / מנוע
    "טורבו-בנזין", "טורבו-דיזל", "בנזין", "דיזל", "טורבו", "היברידי", "היברידי-נטען",
    "מיקרו-היברידי", "פלאג-אין", "סוללת",
    # תיבת הילוכים
    "אוט'", "אוטו'", "ידני", "רובוטית", "כפולת", "מצמדים", "גיר",
    # מרכב / מידות / מושבים
    "מושבים", "מקומות", "נוסעים", "ארוך", "קצר", "גבוה", "נמוך", "בינוני", "סופר",
    "סופר-ארוך", "סופר-גבוה", "קבינה", "חד-קבינה", "חד", "דאבל", "דאבל-קבינה", "מורחבת",
    "קופה", "קבריולה", "סדאן", "ספורטבק", "קומבי", "ואן", "מולטיוואן", "מסחרי", "טנדר",
    "גג", "גובה", "אורך", "משקל", "משלוח", "חישוקי", "מקסי", "קומפקט", "רציף", "סגור",
    "מאוד", "רגיל", "אוואנט", "טון",
}


# ── deterministic name extraction from icar's raw version_name ────────────────────

def _keep_en(tok: str) -> bool:
    """A Latin grade token (Premium, M-Sport, RS, M240i…) — drops numbers, Hebrew, DT codes."""
    if _NUM.match(tok) or _DRIVETRAIN_CODE.match(tok):
        return False
    if re.search(r"[א-ת]", tok):
        return False
    return bool(re.search(r"[A-Za-z]", tok))


def _keep_he(tok: str) -> bool:
    """For the Hebrew Name: keep Latin grade tokens AND meaningful Hebrew (not spec words)."""
    if _NUM.match(tok) or _DRIVETRAIN_CODE.match(tok):
        return False
    if re.search(r"[א-ת]", tok):
        if '"' in tok:                               # a unit token (כ"ס, ק"מ, קוט"ש…)
            return False
        return tok not in _HE_SPEC_DROP
    return bool(re.search(r"[A-Za-z]", tok))         # a Latin grade token


def _sanitize(s: str) -> str:
    """Strip stray quote/bracket artifacts and collapse whitespace."""
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
            for fn in taggers:
                en, he = fn(t)
                if en and en not in t["name_en"]:
                    t["name_en"] = f"{t['name_en']} {en}".strip()
                if he and he not in t["name_he"]:     # skip if icar already supplied it
                    t["name_he"] = f"{t['name_he']} {he}".strip()
        seen: dict[str, int] = {}
        for t in group:
            key = t["name_en"].upper()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                t["name_en"] += f" {seen[key]}"
                t["name_he"] += f" {seen[key]}"


# ── public entry point (deterministic — no AI, no client) ────────────────────────

def clean_trim_names(mfr_en: str, model_en: str, trims: list[dict]) -> None:
    """Set deterministic bilingual names for each trim, in place — straight from icar."""
    if not trims:
        return

    for t in trims:
        raw  = (t.get("name_he") or t.get("name_en") or "").strip()
        toks = raw.split()
        en = _sanitize(" ".join(tok for tok in toks if _keep_en(tok)).strip(" -"))
        he = _sanitize(" ".join(tok for tok in toks if _keep_he(tok)).strip(" -"))
        t["name_en"] = en or "Standard"
        t["name_he"] = he or t["name_en"]            # empty → fall back to the English grade
        t["_raw"]    = raw

    _disambiguate(trims)
    for t in trims:
        t.pop("_raw", None)
    log.info(f"  [naming] {model_en}: " +
             ", ".join(f"{t['name_he']} / {t['name_en']}" for t in trims))
