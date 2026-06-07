"""
agents/scrapers/icar_api.py
Deterministic trim source via icar.co.il's internal JSON API.

icar renders its price/spec tables client-side from these endpoints:
  GET  /api/cars/manufacturers           → [{id, title(he), en_title, importer}]
  GET  /api/cars/version_grid            → every version on the site (23k+ rows)
  POST /api/cars/versions {versions,full}→ full literal specs + coded values per version
  POST /api/cars/compare_table {versions}→ fields_values: the code→label decode map

This gives the COMPLETE, stable trim list per model plus real specs — far better than
asking an LLM to enumerate trims (which is partial and inconsistent). The LLM stays as a
fallback only for models not covered here.

Public entry point: get_trims(mfr_en, mfr_he, model_en, model_he) → runner-compatible dicts.
"""
import datetime
import json
import logging
import re

import httpx

log = logging.getLogger(__name__)

_BASE = "https://www.icar.co.il/api/cars"
_H = {
    "User-Agent": "Mozilla/5.0 (compatible; CarAgentBot/1.0)",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.icar.co.il/",
    "Content-Type": "application/json",
}
_TIMEOUT = 40
_CUR_YEAR = datetime.date.today().year
_MIN_YEAR = _CUR_YEAR - 1          # "current" = this year or last year

# ── in-process caches (refreshed each worker lifetime) ──────────────────────────
_cache: dict = {"manufacturers": None, "grid": None, "fields": None, "models": {}}


def _get(path: str):
    r = httpx.get(f"{_BASE}/{path}", headers=_H, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict):
    r = httpx.post(f"{_BASE}/{path}", headers=_H, content=json.dumps(body), timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _manufacturers() -> list[dict]:
    if _cache["manufacturers"] is None:
        d = _get("manufacturers")
        _cache["manufacturers"] = d.get("data", d) if isinstance(d, dict) else d
    return _cache["manufacturers"]


def _grid() -> list[dict]:
    if _cache["grid"] is None:
        d = _get("version_grid")
        _cache["grid"] = d.get("data", d) if isinstance(d, dict) else d
        log.info(f"[icar-api] version_grid נטען: {len(_cache['grid'])} גרסאות")
    return _cache["grid"]


def _fields_values() -> dict:
    """code→label decode map (global, fetched once via any version)."""
    if _cache["fields"] is None:
        grid = _grid()
        sample = grid[0]["version_id"] if grid else 0
        d = _post("compare_table", {"versions": [sample]})
        _cache["fields"] = (d.get("data") or {}).get("fields_values", {})
    return _cache["fields"]


def _mfr_id(mfr_en: str, mfr_he: str) -> int | None:
    en = (mfr_en or "").strip().lower()
    he = (mfr_he or "").strip()
    for m in _manufacturers():
        if en and (m.get("en_title") or "").strip().lower() == en:
            return m.get("id")
    for m in _manufacturers():
        if he and (m.get("title") or "").strip() == he:
            return m.get("id")
    return None


def manufacturer_on_icar(mfr_en: str, mfr_he: str) -> bool:
    """True if the manufacturer exists on icar — so a missing model is a match gap,
    not genuine absence (only absence justifies the gov.il fallback)."""
    try:
        return _mfr_id(mfr_en, mfr_he) is not None
    except Exception:
        return False


def _is_current(row: dict) -> bool:
    iy = str(row.get("identity_year") or "")
    ye = str(row.get("year_end") or "").strip()
    if iy.isdigit() and int(iy) >= _MIN_YEAR:
        return True
    if ye in ("", "0", "None"):
        return True
    return ye.isdigit() and int(ye) >= _MIN_YEAR


def _current_models(mfr_id: int) -> dict[str, list[int]]:
    """{icar_model_name(he): [current version_ids]} for one manufacturer."""
    out: dict[str, list[int]] = {}
    for row in _grid():
        if row.get("asmanufacturer_id") != mfr_id:
            continue
        if not _is_current(row):
            continue
        name = (row.get("model_name") or "").strip()
        if name:
            out.setdefault(name, []).append(row["version_id"])
    return out


def _resolve_model_name(mfr_en: str, model_en: str, candidates: list[str],
                        model_he: str = "") -> str | None:
    """Pick the icar model name matching this model.

    First a deterministic exact match of the model identifier (model_he or model_en)
    against the candidate list — this handles numeric/identifier model names like RAM
    "2500"/"3500", where the English name is just the brand and the AI cannot match.
    Falls back to a constrained AI pick (cached per mfr_en|model_en) only when needed.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # deterministic exact match — no AI, no false positives
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip()).lower()
    by_norm = {_norm(c): c for c in candidates}
    for probe in (model_he, model_en):
        hit = by_norm.get(_norm(probe))
        if hit:
            return hit

    key = f"{mfr_en}|{model_en}".lower()
    if key in _cache["models"]:
        return _cache["models"][key]

    from ..model_openai.utils import get_client, ai_call, parse_json
    lst = "\n".join(f'{i+1}. {c}' for i, c in enumerate(candidates))
    prompt = f"""Match the car model to its Hebrew name from icar.co.il.

Manufacturer: {mfr_en}
Model (English): {model_en}

Hebrew model names on icar (choose EXACTLY one, or "NONE" if none match):
{lst}

Return ONLY JSON: {{"match": "<exact Hebrew string from the list, or NONE>"}}"""
    try:
        match = (parse_json(ai_call(get_client(), prompt)).get("match") or "").strip()
    except Exception as e:
        log.warning(f"[icar-api] התאמת דגם נכשלה ({model_en}): {e}")
        match = ""
    result = match if match in candidates else None
    _cache["models"][key] = result
    return result


# ── deterministic model-level facts (fuel / first-sold year / seats) from icar ───

_FUEL_CATEGORY = ("חשמלי", "היברידי", "בנזין", "דיזל", "פלאג-אין היברידי")


def _engine_to_fuel(engine_label: str) -> str:
    """Map icar's decoded performance.engine to one canonical fuel category."""
    s = engine_label or ""
    if "נטען" in s:
        return "פלאג-אין היברידי"
    if "חשמלי" in s or "מימן" in s:
        return "חשמלי"
    if "היברידי" in s or "מיקרו" in s:
        return "היברידי"
    if "דיזל" in s:
        return "דיזל"
    if "בנזין" in s:
        return "בנזין"
    return ""


def get_model_facts(mfr_en: str, mfr_he: str, model_en: str, model_he: str) -> dict:
    """Deterministic model-level facts straight from icar (NO AI for the values):
    {fuel, year_from, seats}. fuel = most-common engine fuel of the current line-up,
    year_from = earliest launch year, seats = max seating. {} if the model isn't on icar
    (caller keeps its existing values). Model match prefers an exact Hebrew-name hit."""
    try:
        mid = _mfr_id(mfr_en, mfr_he)
        if not mid:
            return {}
        models = _current_models(mid)
        if not models:
            return {}
        icar_name = None
        for cand in (model_he, model_en):
            c = (cand or "").strip()
            if c and c in models:
                icar_name = c
                break
        if not icar_name:
            icar_name = _resolve_model_name(mfr_en, model_en, list(models.keys()), model_he)
        if not icar_name:
            return {}

        d = _post("versions", {"versions": models[icar_name], "full": True})
        full = d.get("data") or d
        if isinstance(full, dict):
            full = list(full.values())
        fv = _fields_values()

        launches: list[int] = []
        years: list[int] = []
        for v in full:
            yz = str((v.get("identity") or {}).get("year") or "")
            if yz.isdigit():
                years.append(int(yz))
            lz = str((v.get("identity") or {}).get("launch") or "")
            if lz.isdigit():
                launches.append(int(lz))
        cur_year = max(years) if years else None

        import collections
        fuels: collections.Counter = collections.Counter()
        seats: list[int] = []
        for v in full:
            ident = v.get("identity") or {}
            if cur_year and str(ident.get("year") or "") != str(cur_year):
                continue                                   # current line-up only for fuel/seats
            perf = v.get("performance") or {}
            f = _engine_to_fuel(_label(fv, "performance.engine", perf.get("engine")))
            if f:
                fuels[f] += 1
            s = _seats_to_int(_label(fv, "identity.sitting", ident.get("sitting")))
            if s:
                seats.append(s)

        facts: dict = {}
        if fuels:
            facts["fuel"] = fuels.most_common(1)[0][0]
        if launches:
            facts["year_from"] = str(min(launches))
        if seats:
            facts["seats"] = max(seats)
        return facts
    except Exception as e:
        log.warning(f"[icar-api] get_model_facts נכשל עבור {mfr_en} {model_en}: {e}")
        return {}


# ── decode + field mapping ──────────────────────────────────────────────────────

def _label(fv: dict, sect_field: str, code) -> str:
    return fv.get(sect_field, {}).get(str(code), "").strip()


def _num(v):
    """Parse a numeric value from icar (handles '4,117' and '' and floats)."""
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _seats_to_int(label: str):
    """'5'→5, '5+2'→7, '2+2'→4, '6+1'→7."""
    nums = [int(n) for n in re.findall(r"\d+", label or "")]
    return sum(nums) if nums else None


def _drivetrain(label: str) -> str | None:
    if not label:
        return None
    if "4X4" in label or "כפול" in label:
        return "4X4"
    if "קדמית" in label:
        return "קדמית"
    if "אחורית" in label:
        return "אחורית"
    return None


def _map_version(v: dict, fv: dict) -> dict | None:
    """Map one icar full-version record to a runner-compatible trim dict."""
    ident = v.get("identity", {}) or {}
    perf  = v.get("performance", {}) or {}
    size  = v.get("size", {}) or {}

    price = _num(v.get("price") or ident.get("price"))
    name  = (v.get("version_name") or "").strip()
    if not name or price is None:
        return None

    moment = _num(perf.get("moment"))                 # kg·m on icar
    length = _num(size.get("length"))                 # cm on icar
    width  = _num(size.get("width"))
    height = _num(size.get("height"))
    poll   = _label(fv, "performance.pollution", perf.get("pollution"))

    def mm(x):
        return int(round(x * 10)) if x is not None else None

    trim = {
        "name_he":          name,
        "name_en":          "",
        "price":            int(price),
        "price_source":     "icar",
        "source_url":       "https://www.icar.co.il" + (v.get("url") or ""),
        "spec_source_url":  "https://www.icar.co.il" + (v.get("url") or ""),
        "hp":               _num(perf.get("power")),
        "engine_cc":        _num(perf.get("capacity")),
        "torque_nm":        int(round(moment * 9.80665)) if moment else None,
        "sec":              _num(perf.get("acceleration")),
        "top_speed":        _num(perf.get("speed")),
        "fuel_consumption": _num(perf.get("consumption")),
        "battery_kwh":      _num(perf.get("battery_capacity")),
        "range_km":         _num(perf.get("electric_range")),
        "transmission":     _label(fv, "performance.gearbox", perf.get("gearbox")) or None,
        "drivetrain":       _drivetrain(_label(fv, "performance.ignition", perf.get("ignition"))),
        "doors":            _num(_label(fv, "identity.doors", ident.get("doors"))),
        "seats":            _seats_to_int(_label(fv, "identity.sitting", ident.get("sitting"))),
        "pollution_level":  int(poll) if poll.isdigit() else None,
        "length_mm":        mm(length),
        "width_mm":         mm(width),
        "height_mm":        mm(height),
        "trunk_liters":     _num(size.get("cargo")),
        "weight_kg":        _num(size.get("selfweight")),
        "warranty":         _label(fv, "identity.guarantee", ident.get("guarantee")) or None,
    }
    return trim


# ── Public entry point ──────────────────────────────────────────────────────────

def get_trims(mfr_en: str, mfr_he: str, model_en: str, model_he: str) -> list[dict]:
    """
    Return the COMPLETE current trim list for a model straight from icar's API,
    with full specs. Returns [] if the manufacturer/model can't be resolved on icar
    (caller should then fall back to the AI fetcher).
    """
    try:
        mid = _mfr_id(mfr_en, mfr_he)
        if not mid:
            log.info(f"[icar-api] יצרן לא נמצא ב-icar: {mfr_en}")
            return []
        models = _current_models(mid)
        if not models:
            return []
        icar_name = _resolve_model_name(mfr_en, model_en, list(models.keys()), model_he)
        if not icar_name:
            log.info(f"[icar-api] דגם לא הותאם ב-icar: {mfr_en} {model_en}")
            return []

        version_ids = models[icar_name]
        d = _post("versions", {"versions": version_ids, "full": True, "urls": "model"})
        full = d.get("data") or d
        if isinstance(full, dict):
            full = list(full.values())
        fv = _fields_values()

        # icar keeps a record per model-year; the PRICE lives only on the latest
        # priced year. Group by trim name and keep the most-recent record that has a
        # price, so every current trim gets its accurate current price.
        best: dict[str, tuple[int, dict]] = {}
        for v in full:
            name  = (v.get("version_name") or "").strip()
            price = _num(v.get("price") or (v.get("identity") or {}).get("price"))
            if not name or not price:
                continue
            yr = int(str((v.get("identity") or {}).get("year") or 0) or 0)
            if name not in best or yr > best[name][0]:
                best[name] = (yr, v)

        trims = []
        for _, v in best.values():
            t = _map_version(v, fv)
            if t:
                trims.append(t)

        log.info(f"[icar-api] {mfr_en} {model_en} → '{icar_name}': {len(trims)} גרסאות מ-icar (עם מחיר)")
        return trims
    except Exception as e:
        log.warning(f"[icar-api] נכשל עבור {mfr_en} {model_en}: {e}")
        return []
