"""
agents/trim_openai/runner.py
Orchestration: fetch trim levels per model, sync with Zoho (create/update/activate/deactivate).
"""
import logging
import os
import re
import sys

from .fields import CHANGE_FIELDS, FIELD_SPECS

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
    # Map every known field (core + 2026 enrichment) — only when a value is present.
    for field, key, cast in FIELD_SPECS:
        v = trim.get(key)
        if v is None or v == "":
            continue
        try:
            rec[field] = cast(v)
        except (TypeError, ValueError):
            pass
    return rec


def _changed_fields(existing: dict, new_rec: dict) -> dict:
    changes = {}
    for f in CHANGE_FIELDS:
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


def _prefix_tokens(*names: str) -> set[str]:
    """Tokenise manufacturer + model names (both languages) for prefix stripping."""
    toks = set()
    for s in names:
        for w in re.split(r'[\s,\-\.]+', (s or "").strip()):
            if w:
                toks.add(w.upper())
    return toks


def _strip_prefix(name: str, ptoks: set[str]) -> str:
    """Drop leading manufacturer/model tokens: 'Hyundai Ioniq 6 Ultra 2x4' → 'Ultra 2x4'.

    Makes trim matching robust to the AI sometimes prefixing the model name and
    sometimes not — the root cause of duplicate trims.
    """
    if not name:
        return name
    words = name.split()
    i = 0
    while i < len(words) and re.sub(r'[,\-\.]', '', words[i]).upper() in ptoks:
        i += 1
    return " ".join(words[i:]).strip() if i < len(words) else name.strip()


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
                   existing_trims: list[dict], ptoks: set[str]) -> dict | None:
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
        t_en = _strip_prefix((t.get("Car_Finish_level_Name_EN") or "").strip(), ptoks)
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
    from .validator import validate_fetch
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
        "deactivated": 0, "no_change": 0, "rejected": 0, "no_data": 0, "errors": 0,
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
        _snap = {k: stats[k] for k in ("created","updated","activated","deactivated","no_change","rejected","no_data","errors")}

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

        # תחיליות יצרן+דגם לחיתוך, ואינדקס לפי שם מנורמל (חסין-תחילית)
        ptoks = _prefix_tokens(mfr_en, mfr_he, model_en, model_he)
        existing_by_he = {_strip_prefix((t.get("Name") or "").strip(), ptoks).upper(): t for t in existing_trims}
        existing_by_en = {_strip_prefix((t.get("Car_Finish_level_Name_EN") or "").strip(), ptoks).upper(): t for t in existing_trims}

        # ── AI fetch ───────────────────────────────────────
        try:
            ai_trims = get_trims_ai(client, mfr_en, mfr_he, model_en, model_he)
        except Exception as e:
            log.error(f"  [trim-runner] שגיאת AI עבור {model_en}: {e}")
            stats["errors"] += 1
            continue

        # אין מחירים במקורות המאושרים — דילוג שקט, לא שגיאה (לא ממציאים נתונים)
        if not ai_trims:
            log.info(f"  [trim-runner] {model_en}: לא נמצאו מחירים במקורות המאושרים — מדלג")
            stats["no_data"] += 1
            continue

        # ── שער אימות דטרמיניסטי ────────────────────────────
        # בודק שלמות + תקינות לפני כל כתיבה. רק גרסאות שעברו נכתבות.
        vf          = validate_fetch(ai_trims)
        valid_trims = vf["valid"]
        if vf["rejected"]:
            stats["rejected"] += len(vf["rejected"])
            log.warning(f"     [validate] {len(vf['rejected'])} גרסאות נדחו ולא ייכתבו")
        if not valid_trims:
            # נמצאו גרסאות אך כולן נדחו באימות — כבר נספרו ב-rejected, לא שגיאת מערכת
            log.warning(f"  [trim-runner] {model_en}: כל הגרסאות נדחו באימות — לא נכתב דבר")
            continue

        # נירמול קנוני: חיתוך תחילית יצרן+דגם משמות הגרסאות (גם he וגם en),
        # כך שגרסה חדשה תיכתב נקייה ותותאם לקיימת ללא כפילות.
        for trim in valid_trims:
            trim["name_he"] = _strip_prefix((trim.get("name_he") or "").strip(), ptoks)
            trim["name_en"] = _strip_prefix((trim.get("name_en") or "").strip(), ptoks)

        # מיפוי שמות הגרסאות שאומתו (כולל נורמליזציה) לצורך בדיקת כיבוי
        ai_he_set = _ai_norm_set(valid_trims, "name_he")
        ai_en_set = _ai_norm_set(valid_trims, "name_en")

        # ── כתיבה ל-Zoho ────────────────────────────────────
        resolved_ids = set()

        for trim in valid_trims:
            name_he = (trim.get("name_he") or "").strip()
            name_en = (trim.get("name_en") or "").strip()
            if not name_he:
                continue

            existing = _find_existing(
                name_he, name_en,
                existing_by_he, existing_by_en, existing_trims, ptoks
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
        log.info(f"  └── {model_en}: +{_d('created')} ↑{_d('activated')} ~{_d('updated')} ↓{_d('deactivated')} ={_d('no_change')} ✗{_d('rejected')} ∅{_d('no_data')} !{_d('errors')}")

        # ── כיבוי גרסאות שלא נמצאו ─────────────────────────
        # ריסון churn: מכבים רק כששליפה "מקיפה" — החזירה לפחות כמה גרסאות
        # כמו שכבר פעילות. ריצה דלילה (פחות גרסאות מהקיים) לא מהימנה לכיבוי.
        active_existing = sum(1 for t in existing_trims if t.get("Active", True))
        comprehensive   = len(valid_trims) >= active_existing
        if zoho_get_ok and vf["complete"] and not comprehensive:
            log.info(f"     [deactivate] דילוג — שליפה דלילה ({len(valid_trims)} גרסאות < {active_existing} פעילות)")
        if zoho_get_ok and vf["complete"] and comprehensive:
            for t in existing_trims:
                if t.get("id") in resolved_ids:
                    continue
                t_he = _strip_prefix((t.get("Name") or "").strip(), ptoks).upper()
                t_en = _strip_prefix((t.get("Car_Finish_level_Name_EN") or "").strip(), ptoks).upper()
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
        f"↓{stats['deactivated']} ={stats['no_change']} ✗{stats['rejected']} ∅{stats['no_data']} !{stats['errors']}"
    )
    return stats
