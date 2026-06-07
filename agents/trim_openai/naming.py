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
# Map the authoritative icar fuel (trim["fuel"]) → (English tag, Hebrew tag) for
# disambiguation. Uses the SAME value as the Fuel_Type field, so a name never disagrees
# with the fuel (e.g. a mild-hybrid petrol is never tagged "היברידי").
_FUEL_TAG = {
    "בנזין":            ("Petrol", "בנזין"),
    "דיזל":             ("Diesel", "דיזל"),
    "היברידי":          ("Hybrid", "היברידי"),
    "פלאג-אין היברידי": ("PHEV",   "היברידי נטען"),
    "חשמלי":            ("EV",     "חשמלי"),
}
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

def _liters(t: dict) -> str:
    cc = t.get("engine_cc")
    if not cc:
        return ""
    try:
        return f"{float(cc) / 1000:.1f}".rstrip("0").rstrip(".") + "L"
    except (TypeError, ValueError):
        return ""


def _tag_fuel(t):    return _FUEL_TAG.get(t.get("fuel") or "", ("", ""))  # authoritative icar fuel
def _tag_liters(t):  l = _liters(t); return (l, l)
def _tag_dt(t):      d = t.get("drivetrain") or ""; return (_DT_EN.get(d, ""), d)


def _tag_seats(t):
    s = t.get("seats")
    if not s:
        return ("", "")
    try:
        n = int(s)
    except (TypeError, ValueError):
        return ("", "")
    return (f"{n} Seats", f"{n} מושבים")


def _tag_battery(t):
    b = t.get("battery_kwh")
    if not b:
        return ("", "")
    try:
        v = f"{int(round(float(b)))}kWh"
    except (TypeError, ValueError):
        return ("", "")
    return (v, v)


def _tag_power(t):
    h = t.get("hp")
    if not h:
        return ("", "")
    try:
        n = int(float(h))
    except (TypeError, ValueError):
        return ("", "")
    return (f"{n}hp", f'{n} כ"ס')


# Priority order for disambiguating same-grade trims by a REAL differing attribute.
# Engine (fuel → displacement) first, then drivetrain, power, seats, battery. Never a bare
# counter — if two trims differ, the difference must be a real spec.
_TAGGERS = [_tag_fuel, _tag_liters, _tag_dt, _tag_power, _tag_seats, _tag_battery]


def _disambiguate(trims: list[dict]) -> list[dict]:
    """Make trim names unique using real differing attributes, in priority order.

    Each language is uniquified independently: a tag is appended only to the language that
    actually collides (so when icar's Hebrew already separates two trims, e.g.
    'הנעה אחורית' vs 'הנעה כפולה', no redundant tag is added). Tags are real attributes
    (fuel → drivetrain → engine size → seats → battery → power) — never a bare counter.
    Trims identical on every real attribute are true duplicates and are collapsed to one
    (lowest real price). Returns the de-duplicated list.
    """
    def uniquify(field: str, idx: int) -> None:
        groups: dict[str, list[dict]] = {}
        for t in trims:
            groups.setdefault((t.get(field) or "").strip().upper(), []).append(t)
        for group in groups.values():
            if len(group) < 2:
                continue
            for fn in _TAGGERS:
                if len({(t[field] or "").strip().upper() for t in group}) == len(group):
                    break                                   # already fully distinct
                if len({fn(t)[idx] for t in group}) <= 1:
                    continue                                # doesn't separate this group
                for t in group:
                    tag = fn(t)[idx]
                    if tag and tag not in t[field]:
                        t[field] = f"{t[field]} {tag}".strip()

    uniquify("name_en", 0)
    uniquify("name_he", 1)

    # collapse true duplicates (identical en+he after tagging) — keep the lowest real price
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for t in trims:
        key = ((t.get("name_en") or "").strip().upper(),
               (t.get("name_he") or "").strip().upper())
        if key not in seen:
            seen[key] = t
            out.append(t)
        else:
            kept = seen[key]
            kp, tp = kept.get("price") or 0, t.get("price") or 0
            if tp and (not kp or tp < kp):
                out[out.index(kept)] = t
                seen[key] = t
    return out


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

    trims[:] = _disambiguate(trims)         # unique names + collapse true duplicates, in place
    for t in trims:
        t.pop("_raw", None)
    log.info(f"  [naming] {model_en}: " +
             ", ".join(f"{t['name_he']} / {t['name_en']}" for t in trims))
