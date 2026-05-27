"""
agents/trim_openai/runner.py
Orchestration: fetch trim levels per model, sync with Zoho (create/update/activate/deactivate).
"""
import logging
import os
import re
import sys

log = logging.getLogger(__name__)


def _zoho():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    import zoho_client
    return zoho_client


def _build_zoho_trim(trim: dict, mfr_id: str, model_id: str) -> dict:
    rec = {
        "Name":                     trim.get("name_he", ""),
        "Car_Finish_level_Name_EN": trim.get("name_en", ""),
        "Manufacturer":             mfr_id,
        "Model":                    model_id,
        "Active":                   True,
    }
    for field, key, cast in [
        ("Unit_Price",   "price",       int),
        ("sec",          "sec",         float),
        ("Top_Speed",    "top_speed",   int),
        ("Screen_inch",  "screen_inch", float),
        ("Doors",        "doors",       int),
        ("Seats",        "seats",       int),
    ]:
        if trim.get(key) is not None:
            try:
                rec[field] = cast(trim[key])
            except (TypeError, ValueError):
                pass

    if trim.get("hp") is not None:
        try:
            rec["HP"] = str(int(trim["hp"]))
        except (TypeError, ValueError):
            pass

    if trim.get("range_km") is not None:
        rec["Range_km"] = str(trim["range_km"])

    return rec


def _changed_fields(existing: dict, new_rec: dict) -> dict:
    check = [
        "Unit_Price", "HP", "sec", "Range_km",
        "Top_Speed", "Screen_inch", "Doors", "Seats", "Car_Finish_level_Name_EN",
    ]
    changes = {}
    for f in check:
        if f not in new_rec:
            continue
        if str(existing.get(f, "")) != str(new_rec[f]):
            changes[f] = new_rec[f]
    return changes


# ── Matching helpers ──────────────────────────────────────────────────────────

def _norm(name: str) -> str:
    """Strip year suffixes and normalise for comparison."""
    s = re.sub(r'\b20\d{2}\b', '', name)   # remove 2024, 2025, 2026 …
    s = re.sub(r'[,\-\.]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s.upper()


def _he_ok(name: str) -> bool:
    """True if the Name field contains real Hebrew (not Zoho's ????? corruption)."""
    return any('א' <= c <= 'ת' for c in name)


def _en_words(name: str) -> set[str]:
    # ASCII-only words so Hebrew tokens don't dilute overlap score
    return {w for w in _norm(name).split() if len(w) >= 2 and w.isascii()}


def _overlap(a: str, b: str) -> float:
    wa, wb = _en_words(a), _en_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _find_existing(name_he: str, name_en: str,
                   existing_by_he: dict, existing_by_en: dict,
                   existing_trims: list[dict]) -> dict | None:
    # 1. Exact Hebrew match (skip if corrupted ???? values)
    if _he_ok(name_he):
        m = existing_by_he.get(name_he.upper())
        if m:
            return m

    # 2. Exact English match
    m = existing_by_en.get(name_en.upper())
    if m:
        return m

    # 3. Normalised (year-stripped) matches
    norm_he = _norm(name_he)
    norm_en = _norm(name_en)

    for key, val in existing_by_he.items():
        if _he_ok(key) and _norm(key) == norm_he:
            return val
    for key, val in existing_by_en.items():
        if _norm(key) == norm_en:
            return val

    # 4. Word-overlap on English names (≥ 0.7) — handles "Boost" vs "Boost 2026"
    best_score, best_match = 0.0, None
    for t in existing_trims:
        t_en = (t.get("Car_Finish_level_Name_EN") or "").strip()
        if not t_en:
            continue
        score = _overlap(name_en, t_en)
        if score > best_score:
            best_score, best_match = score, t
    if best_score >= 0.70:
        _men = best_match.get("Car_Finish_level_Name_EN")
        log.info(f"  [match] '{name_en}' -> '{_men}' (overlap {best_score:.0%})")
        return best_match

    return None


def _ai_norm_set(trims: list[dict], key: str) -> set[str]:
    """Normalised set of trim names from AI output (for deactivation check)."""
    s = set()
    for t in trims:
        v = (t.get(key) or "").strip()
        if v:
            s.add(v.upper())
            s.add(_norm(v))
    return s


# ── Main runner ───────────────────────────────────────────────────────────────

def run(payload) -> dict:
    from .fetcher import get_trims_ai
    from ..model_openai.utils import get_client

    client = get_client()
    zc     = _zoho()

    if not isinstance(payload, dict):
        log.error("[trim-runner] payload לא תקין")
        return {"error": "invalid payload"}

    mfr    = payload.get("manufacturer") or {}
    mfr_id = str(mfr.get("id", ""))
    mfr_en = mfr.get("name_en") or mfr.get("name") or ""
    mfr_he = mfr.get("name_he") or ""
    models = payload.get("models", [])

    log.info("═" * 48)
    log.info(f"[trim-runner] יצרן: {mfr_en} ({mfr_he}) | {len(models)} דגמים")
    log.info("═" * 48)

    stats = {
        "manufacturer": mfr_en,
        "models": len(models),
        "created": 0, "updated": 0, "activated": 0,
        "deactivated": 0, "no_change": 0, "errors": 0,
    }

    for model in models:
        model_id = str(model.get("id", ""))
        model_en = model.get("name_en") or model.get("Name") or ""
        model_he_raw = model.get("name_he") or model.get("Car_Model_Name_HE") or ""
        model_he = model_he_raw if any("֐" <= c <= "׿" for c in model_he_raw) else ""

        if not model_id:
            log.warning(f"  [trim-runner] דגם ללא ID — מדלג: {model_en}")
            continue

        log.info(f"")
        log.info(f"  ┌── {model_en}{(' (' + model_he + ')') if model_he else ''}")
        _snap = {k: stats[k] for k in ("created","updated","activated","deactivated","no_change","errors")}

        # ── שלוף קיים מ-Zoho ──────────────────────────────
        existing_trims: list[dict] = []
        zoho_get_ok = False
        try:
            existing_trims = zc.get_trims(model_id)
            zoho_get_ok = True
            log.info(f"     {len(existing_trims)} גרסאות קיימות ב-Zoho")
        except Exception as e:
            log.warning(f"  [trim-runner] get_trims נכשל: {e}")
            stats["errors"] += 1
            continue

        # אינדקס לפי שם
        existing_by_he = {(t.get("Name") or "").strip().upper(): t for t in existing_trims}
        existing_by_en = {(t.get("Car_Finish_level_Name_EN") or "").strip().upper(): t for t in existing_trims}

        # ── AI fetch ───────────────────────────────────────
        try:
            ai_trims = get_trims_ai(client, mfr_en, mfr_he, model_en, model_he)
        except Exception as e:
            log.error(f"  [trim-runner] שגיאת AI עבור {model_en}: {e}")
            stats["errors"] += 1
            continue

        # מיפוי שמות AI (כולל נורמליזציה) לצורך בדיקת כיבוי
        ai_he_set = _ai_norm_set(ai_trims, "name_he")
        ai_en_set = _ai_norm_set(ai_trims, "name_en")

        # ── כתיבה ל-Zoho ────────────────────────────────────
        resolved_ids = set()

        for trim in ai_trims:
            name_he = (trim.get("name_he") or "").strip()
            name_en = (trim.get("name_en") or "").strip()
            if not name_he:
                continue

            existing = _find_existing(
                name_he, name_en,
                existing_by_he, existing_by_en, existing_trims
            )

            new_rec = _build_zoho_trim(trim, mfr_id, model_id)

            if not existing:
                try:
                    zc.create_trim(new_rec)
                    log.info(f"  [+] {name_he} — ₪{trim.get('price', 0):,}")
                    stats["created"] += 1
                except Exception as e:
                    log.error(f"  [trim] שגיאה ביצירה '{name_he}': {e}")
                    stats["errors"] += 1
            else:
                resolved_ids.add(existing.get("id"))
                was_active = existing.get("Active", True)
                changes = _changed_fields(existing, new_rec)

                if not was_active:
                    changes["Active"] = True
                    try:
                        zc.update_trim(existing["id"], changes)
                        log.info(f"  [↑] הופעל: {name_he}")
                        stats["activated"] += 1
                    except Exception as e:
                        log.error(f"  [trim] שגיאה בהפעלה '{name_he}': {e}")
                        stats["errors"] += 1
                elif changes:
                    try:
                        zc.update_trim(existing["id"], changes)
                        log.info(f"  [~] {name_he} | עודכן: {list(changes.keys())}")
                        stats["updated"] += 1
                    except Exception as e:
                        log.error(f"  [trim] שגיאה בעדכון '{name_he}': {e}")
                        stats["errors"] += 1
                else:
                    log.info(f"  [=] {name_he} — ללא שינוי")
                    stats["no_change"] += 1

        _d = lambda k: stats[k] - _snap[k]
        log.info(f"  └── {model_en}: +{_d('created')} ↑{_d('activated')} ~{_d('updated')} ↓{_d('deactivated')} ={_d('no_change')} !{_d('errors')}")

        # ── כיבוי גרסאות שלא ב-AI ─────────────────────────
        if zoho_get_ok and ai_trims:
            for t in existing_trims:
                if t.get("id") in resolved_ids:
                    continue
                t_he = (t.get("Name") or "").strip().upper()
                t_en = (t.get("Car_Finish_level_Name_EN") or "").strip().upper()
                # Check both raw and normalised names
                if (t_he in ai_he_set or _norm(t_he) in ai_he_set
                        or t_en in ai_en_set or _norm(t_en) in ai_en_set):
                    continue
                if not t.get("Active", True):
                    continue
                try:
                    zc.update_trim(t["id"], {"Active": False})
                    log.info(f"  [↓] כובה: {t.get('Name', '')} / {t_en} (לא נמצא ב-AI)")
                    stats["deactivated"] += 1
                except Exception as e:
                    log.error(f"  [trim] שגיאה בכיבוי: {e}")
                    stats["errors"] += 1

    log.info("═" * 48)
    log.info(
        f"[trim-runner] {mfr_en}: "
        f"+{stats['created']} ↑{stats['activated']} ~{stats['updated']} "
        f"↓{stats['deactivated']} ={stats['no_change']} !{stats['errors']}"
    )
    return stats
