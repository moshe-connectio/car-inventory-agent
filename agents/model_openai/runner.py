"""
agents/model_openai/runner.py
Orchestration: compare existing Zoho records with Israel market data, then create/activate/deactivate.
"""
import json
import re
import logging

from .utils import CAR_TYPES, get_client, validate_record, strip_manufacturer_prefix
from .icar import get_israel_models, _he_prefix_match
from .details import get_details
from .images import get_image_carimagesapi, get_image_icar, is_stored_placeholder

log = logging.getLogger(__name__)

# Import Zoho helpers at call time to avoid import-order issues
def _zoho():
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from zoho_client import create_model, update_model
    return create_model, update_model


# ── main entry point ──────────────────────────────────────────────────────────

def run(payload) -> dict:
    """
    payload: {"manufacturer": {"id","name_en","name_he"}, "models": [...Zoho records...]}
    Also accepts flat list (backwards compat).
    """
    create_model, update_model = _zoho()

    if isinstance(payload, list):
        existing = payload
        mfr_obj  = (existing[0].get("Manufacturer") or {}) if existing else {}
        mfr_en   = mfr_obj.get("name") or mfr_obj.get("Car_Manufacturer_Name_EN") or "Unknown"
        mfr_he   = mfr_obj.get("Car_Manufacturer_Name_HE") or ""
        mfr_id   = mfr_obj.get("id") or ""
    else:
        mfr_obj  = payload.get("manufacturer") or {}
        mfr_en   = mfr_obj.get("name_en") or mfr_obj.get("name") or mfr_obj.get("Car_Manufacturer_Name_EN") or "Unknown"
        mfr_he   = mfr_obj.get("name_he") or mfr_obj.get("Car_Manufacturer_Name_HE") or ""
        mfr_id   = mfr_obj.get("id") or mfr_obj.get("manufacturer_id") or ""
        existing = payload.get("models", [])

    log.info("══════════════════════════════════════════════")
    log.info(f"יצרן: {mfr_en} ({mfr_he}) | דגמים קיימים: {len(existing)}")
    log.info("══════════════════════════════════════════════")

    # ── normalize existing names (strip manufacturer prefix) ───────────────────
    # Must run before matching dicts are built so clean names flow through everywhere.
    _rename_updates: list[tuple[str, dict, str]] = []
    for m in existing:
        orig_en = m.get("Name", "")
        orig_he = m.get("Car_Model_Name_HE", "")
        clean_en = strip_manufacturer_prefix(orig_en, mfr_en, mfr_he)
        clean_he = strip_manufacturer_prefix(orig_he, mfr_en, mfr_he) if orig_he else orig_he
        update: dict = {}
        if clean_en != orig_en:
            update["Name"] = clean_en
            m["Name"] = clean_en
        if clean_he != orig_he:
            update["Car_Model_Name_HE"] = clean_he
            m["Car_Model_Name_HE"] = clean_he
        if update and m.get("id"):
            _rename_updates.append((m["id"], update, orig_en))

    for mid, update, old_name in _rename_updates:
        try:
            update_model(mid, update)
            log.info(f"  [name-clean] ✅ '{old_name}' → {update}")
        except Exception as e:
            log.error(f"  [name-clean] ❌ {old_name}: {e}")

    client = get_client()

    # ── pulse 1 ────────────────────────────────────────────────
    log.info("[pulse-1] מחפש דגמים בשוק הישראלי...")
    israel_models, icar_used = get_israel_models(client, mfr_en, mfr_he, existing)

    if not israel_models:
        if icar_used:
            return {"error": "icar לא מצא דגמים — לא מבצע שינויים", "manufacturer": mfr_en}
        log.info("[pulse-1] AI לא מצא דגמים פעילים — בודק כיבויים לכל הדגמים הקיימים")

    labels = [m.get("name_en") or m.get("name_he") for m in israel_models]
    log.info(f"[pulse-1] נמצאו {len(israel_models)}: {labels}")

    # ── compare ────────────────────────────────────────────────
    existing_by_name = {m.get("Name", ""): m for m in existing}
    existing_by_he   = {(m.get("Car_Model_Name_HE") or "").upper(): m for m in existing}

    icar_base_to_idx: dict[str, int] = {}
    for i, m in enumerate(israel_models):
        he = m.get("name_he", "")
        parts = he.split(" ", 1)
        base = parts[1] if len(parts) == 2 else he
        if base:
            icar_base_to_idx[base.upper()] = i

    israel_names_canonical: set[str] = set()
    icar_resolved: set[int] = set()

    for i, m in enumerate(israel_models):
        if m.get("name_en") and m["name_en"] in existing_by_name:
            israel_names_canonical.add(m["name_en"])
            icar_resolved.add(i)
        elif m.get("name_he"):
            he_upper = m["name_he"].upper()
            parts    = m["name_he"].split(" ", 1)
            base_he  = parts[1].upper() if len(parts) == 2 else he_upper
            rec = existing_by_he.get(he_upper) or existing_by_he.get(base_he)
            if rec:
                israel_names_canonical.add(rec["Name"])
                m["name_en"] = rec["Name"]
                icar_resolved.add(i)

    for existing_rec in existing:
        en_name = existing_rec.get("Name", "")
        he_name = (existing_rec.get("Car_Model_Name_HE") or "").upper()
        if en_name not in israel_names_canonical:
            best_base, best_idx = "", -1
            for base, idx in icar_base_to_idx.items():
                if _he_prefix_match(he_name, base) and len(base) > len(best_base):
                    best_base, best_idx = base, idx
            if best_idx >= 0:
                israel_names_canonical.add(en_name)
                icar_resolved.add(best_idx)
                log.info(f"  [prefix-match] {en_name} ({he_name}) → icar base '{best_base}'")

    # Layer 3.5: image-slug match — English name from icar image filename.
    # Bridges Hebrew icar slugs (e.g. "טראקס") to English Zoho names (e.g. "Trax")
    # by reading the English slug embedded in the image URL (chevrolet-trax-new.jpg).
    def _slug_to_base(img_url: str) -> str:
        if not img_url:
            return ""
        fname = img_url.rsplit("/", 1)[-1].lower()
        slug = re.sub(r'\.(jpg|png|webp)$', '', fname)
        slug = re.sub(r'-new$', '', slug)
        slug = re.sub(r'-\d{4}$', '', slug)  # strip year suffix (-2020, etc.)
        for pfx in [
            mfr_en.lower().replace(" ", "-") + "-",
            mfr_en.lower().replace(" ", "") + "-",  # e.g. "landrover-"
        ]:
            if slug.startswith(pfx):
                slug = slug[len(pfx):]
                break
        return slug.replace("-", " ")

    existing_base_lower: dict[str, str] = {}
    for n in existing_by_name:
        base = n.lower()
        pfx_en = mfr_en.lower() + " "
        if base.startswith(pfx_en):
            base = base[len(pfx_en):]
        existing_base_lower[base] = n

    for i, m in enumerate(israel_models):
        if i in icar_resolved:
            continue
        slug = _slug_to_base(m.get("image_url", ""))
        if slug and slug in existing_base_lower:
            matched_en = existing_base_lower[slug]
            israel_names_canonical.add(matched_en)
            icar_resolved.add(i)
            m["name_en"] = matched_en
            log.info(f"  [slug-match] {m.get('name_he')} → {matched_en} (slug='{slug}')")

    # Layer 4: Signature fallback: match by (numbers, word-count-after-brand).
    # Needed when Car_Model_Name_HE is corrupted in Zoho (returns '?' for Hebrew).
    # English Name field is always reliable; numbers and word-count survive corruption.
    def _sig(name: str) -> tuple | None:
        parts = name.strip().split(None, 1)
        base  = parts[1] if len(parts) > 1 else ""
        if not base:
            return None
        nums  = frozenset(re.findall(r'\b\d+\b', base))
        return (nums, len(base.split()))

    unmatched_en: dict[tuple, str] = {}
    for n in existing_by_name:
        if n not in israel_names_canonical:
            s = _sig(n)
            if s is None:
                continue
            if s in unmatched_en:
                unmatched_en[s] = ""   # ambiguous — mark empty so we skip it
            else:
                unmatched_en[s] = n

    for i, m in enumerate(israel_models):
        if i in icar_resolved:
            continue
        s = _sig(m.get("name_he", ""))
        if s and unmatched_en.get(s):
            matched_en = unmatched_en[s]
            israel_names_canonical.add(matched_en)
            icar_resolved.add(i)
            m["name_en"] = matched_en
            log.info(f"  [sig-match] {m.get('name_he')} → {matched_en}")

    # Layer 5: word-overlap — catches name differences within the same script (same-language text)
    def _normalize_words(name: str) -> set[str]:
        words = re.sub(r"[\-\.,'\"]", " ", name).split()
        return {w.upper() for w in words if len(w) >= 2}

    def _name_overlap(a: str, b: str) -> float:
        wa, wb = _normalize_words(a), _normalize_words(b)
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / max(len(wa), len(wb))

    unmatched_en_names = [n for n in existing_by_name if n not in israel_names_canonical]
    for i, m in enumerate(israel_models):
        if i in icar_resolved:
            continue
        name_he = m.get("name_he", "")
        best_score, best_en = 0.0, ""
        for en_name in unmatched_en_names:
            score = _name_overlap(name_he, en_name)
            if score > best_score:
                best_score, best_en = score, en_name
        if best_score >= 0.55 and best_en:
            israel_names_canonical.add(best_en)
            icar_resolved.add(i)
            m["name_en"] = best_en
            log.info(f"  [word-match] {name_he} → {best_en} (score={best_score:.2f})")

    # Layer 5.5: manufacturer-prefix reconciliation
    # Protects "Limo" when "EVEASY Limo" is already matched (same model, two naming styles).
    pfx_en_lower = mfr_en.lower() + " "
    extra_canonical: set[str] = set()
    for canon in list(israel_names_canonical):
        stripped = canon[len(mfr_en) + 1:] if canon.lower().startswith(pfx_en_lower) else None
        full     = mfr_en + " " + canon
        if stripped and stripped in existing_by_name:
            extra_canonical.add(stripped)
            log.info(f"  [prefix-reconcile] מגן על '{stripped}' (alias של '{canon}')")
        if full in existing_by_name:
            extra_canonical.add(full)
            log.info(f"  [prefix-reconcile] מגן על '{full}' (alias של '{canon}')")
    israel_names_canonical.update(extra_canonical)

    to_create = [m for i, m in enumerate(israel_models) if i not in icar_resolved]
    to_activate = [existing_by_name[n] for n in existing_by_name
                   if n in israel_names_canonical and not existing_by_name[n].get("Active", True)]
    to_deactivate_cands = [existing_by_name[n] for n in existing_by_name
                           if n not in israel_names_canonical and existing_by_name[n].get("Active", False)]
    no_change = [existing_by_name[n] for n in existing_by_name
                 if n in israel_names_canonical and existing_by_name[n].get("Active", True)]

    # Guard: if a to_create item resembles a to_deactivate_cand → update instead of replace.
    # Prevents orphaning trim levels linked to the existing model record.
    to_update_name_he: list[tuple[str, str]] = []
    for nm in to_create[:]:
        name_he_new = nm.get("name_he", "")
        for cand in to_deactivate_cands[:]:
            he_stored = cand.get("Car_Model_Name_HE") or cand.get("Name", "")
            score = _name_overlap(name_he_new, he_stored)
            if score >= 0.55:
                log.warning(
                    f"  [guard] לא מכבה '{cand['Name']}' — דומה מאוד ל-'{name_he_new}' "
                    f"(score={score:.2f}) — מעדכן במקום"
                )
                nm["name_en"] = cand["Name"]
                israel_names_canonical.add(cand["Name"])
                if name_he_new and name_he_new != he_stored:
                    to_update_name_he.append((cand["id"], name_he_new))
                to_create.remove(nm)
                to_deactivate_cands.remove(cand)
                break

    log.info(f"  ליצירה:  {[m.get('name_en') or m.get('name_he') for m in to_create]}")
    log.info(f"  להדלקה: {[m['Name'] for m in to_activate]}")
    log.info(f"  לכיבוי (בבדיקה): {[m['Name'] for m in to_deactivate_cands]}")
    log.info(f"  ללא שינוי: {len(no_change)}")

    # ── confirm ────────────────────────────────────────────────
    # icar is authoritative — no AI confirmation step needed
    confirmed_on  = {m["Name"] for m in to_activate}
    confirmed_off = {m["Name"] for m in to_deactivate_cands}
    if confirmed_off:
        log.info(f"[confirm] כיבוי ישיר: {confirmed_off}")

    # ── build new records ──────────────────────────────────────
    new_records = []
    for nm in to_create:
        name_en = nm.get("name_en", "")
        name_he = nm.get("name_he", "")

        # Pre-extract English from icar image slug when name_en is missing
        if not name_en and nm.get("image_url"):
            slug = _slug_to_base(nm["image_url"])
            if slug and all(c.isascii() for c in slug) and slug.strip():
                name_en = strip_manufacturer_prefix(slug.title(), mfr_en, mfr_he)
                nm["name_en"] = name_en
                log.info(f"  [slug-en] extracted '{name_en}' from image slug")

        label   = name_en or name_he
        log.info(f"[build] {label}...")

        record = get_details(client, mfr_en, name_en, name_he, mfr_id)
        name_en = record.get("Name", name_en)
        name_he = record.get("Car_Model_Name_HE", name_he)
        issues = validate_record(record)
        if issues:
            log.warning(f"  ⚠ {name_en} ולידציה: {issues}")

        # primary: carimagesapi; fall back to icar's image ONLY when carimagesapi has no
        # real image (returns '' for its placeholder).
        image_url = get_image_carimagesapi(mfr_en, name_en)
        if not image_url and nm.get("image_url"):
            log.info(f"  [image] {label}: אין תמונה אמיתית ב-carimagesapi — נופל ל-icar")
            image_url = get_image_icar(mfr_en, name_en, nm["image_url"])
        record["Model_Image_URL"] = image_url
        log.info(f"  תמונה: {image_url or '(לא נמצאה)'}")
        log.info(f"  קטגוריות: {record.get('Category', [])}")
        new_records.append(record)

    # ── execute ────────────────────────────────────────────────
    stats = {
        "manufacturer": mfr_en,
        "mfr_id":       mfr_id,
        "mfr_he":       mfr_he,
        "created": 0, "activated": 0, "deactivated": 0,
        "no_change": len(no_change), "errors": 0,
        "changes": [],
    }

    for record in new_records:
        name = record.get("Name", "")
        try:
            result = create_model(record)
            try:
                zoho_id = (result.get("message") or {}).get("id") or result.get("id")
                if not zoho_id:
                    output  = json.loads(result.get("details", {}).get("output", "{}"))
                    zoho_id = output.get("id") or (output.get("message") or {}).get("id")
            except Exception:
                zoho_id = None
            stats["created"] += 1
            stats["changes"].append({"action": "created", "name": name,
                                     "name_he": record.get("Car_Model_Name_HE", ""),
                                     "id": zoho_id,
                                     "image": record.get("Model_Image_URL", ""),
                                     "category": record.get("Category", [])})
            log.info(f"  ✅ נוצר: {name} (id={zoho_id})")
        except Exception as e:
            stats["errors"] += 1
            log.error(f"  ❌ create {name}: {e}")

    for m in to_activate:
        name = m.get("Name", "")
        if name not in confirmed_on:
            stats["no_change"] += 1
            log.info(f"  ⏭ הדלקה בוטלה (לא אושרה): {name}")
            continue
        try:
            update_model(m["id"], {"Active": True})
            stats["activated"] += 1
            stats["changes"].append({"action": "activated", "name": name,
                                     "name_he": m.get("Car_Model_Name_HE", ""),
                                     "id": m["id"]})
            log.info(f"  ✅ הודלק: {name}")
        except Exception as e:
            stats["errors"] += 1
            log.error(f"  ❌ activate {name}: {e}")

    for m in to_deactivate_cands:
        name = m.get("Name", "")
        if name not in confirmed_off:
            stats["no_change"] += 1
            continue
        try:
            update_model(m["id"], {"Active": False})
            stats["deactivated"] += 1
            stats["changes"].append({"action": "deactivated", "name": name,
                                     "name_he": m.get("Car_Model_Name_HE", ""),
                                     "id": m["id"]})
            log.info(f"  🔴 כובה: {name}")
        except Exception as e:
            stats["errors"] += 1
            log.error(f"  ❌ deactivate {name}: {e}")

    # ── image updates for existing models ─────────────────────
    # icar image per model name (for the carimagesapi → icar fallback)
    icar_img_by_name: dict[str, str] = {}
    for im in israel_models:
        img = im.get("image_url")
        if not img:
            continue
        for key in (im.get("name_en", ""), im.get("name_he", "")):
            if key:
                icar_img_by_name.setdefault(key.strip().upper(), img)

    for m in existing:
        name    = m.get("Name", "")
        name_he = m.get("Car_Model_Name_HE", "")
        if not m.get("Active", True) or name in confirmed_off:
            continue
        mid     = m.get("id", "")
        current = m.get("Model_Image_URL", "")

        if (current or "").startswith("https://images.gsmdev.co.il/car-images/"):
            if not is_stored_placeholder(current):
                log.info(f"  [image-fix] ⏭  {name} — תמונה קיימת על השרת, מדלג")
                continue
            log.info(f"  [image-fix] 🩹 {name} — התמונה השמורה היא placeholder ישן, מרענן")
        log.info(f"  [image-fix] 🔍 {name} — מחפש ב-carimagesapi...")
        new_img = get_image_carimagesapi(mfr_en, name)
        if not new_img:
            icar_url = (icar_img_by_name.get(name.strip().upper())
                        or icar_img_by_name.get((name_he or "").strip().upper()))
            if icar_url:
                log.info(f"  [image-fix] אין ב-carimagesapi — נופל ל-icar")
                new_img = get_image_icar(mfr_en, name, icar_url)
        if new_img:
            try:
                update_model(mid, {"Model_Image_URL": new_img})
                log.info(f"  [image-fix] ✅ עודכן (carimagesapi): {name} → {new_img[:70]}")
                stats["changes"].append({"action": "image_updated", "name": name,
                                         "name_he": name_he, "image": new_img})
            except Exception as e:
                stats["errors"] += 1
                log.error(f"  [image-fix] ❌ {name}: {e}")
        else:
            log.warning(f"  [image-fix] ❌ {name} — לא נמצאה תמונה ב-carimagesapi")
            try:
                update_model(mid, {"System_Alert": "לא נמצאה תמונה לדגם"})
            except Exception as e:
                log.error(f"  [image-fix] ❌ System_Alert {name}: {e}")

    # ── field fixes for existing models ───────────────────────
    fields_updated = 0
    for m in existing:
        name    = m.get("Name", "")
        name_he = m.get("Car_Model_Name_HE", "")
        mid     = m.get("id", "")
        if not mid or not name:
            continue
        missing = {}
        if not m.get("Year_From"):
            missing["Year_From"] = True
        if not m.get("Car_Type") or m.get("Car_Type") not in CAR_TYPES:
            missing["Car_Type"] = True
        if not m.get("Category"):
            missing["Category"] = True
        if not missing:
            continue
        log.info(f"  [field-fix] {name}: חסרים {list(missing.keys())} — מושך פרטים...")
        try:
            details = get_details(client, mfr_en, name, name_he, mfr_id)
            update_data = {}
            if "Year_From" in missing and details.get("Year_From"):
                update_data["Year_From"] = details["Year_From"]
            if "Car_Type" in missing and details.get("Car_Type") in CAR_TYPES:
                update_data["Car_Type"] = details["Car_Type"]
            if "Category" in missing and details.get("Category"):
                update_data["Category"] = details["Category"]
            if update_data:
                update_model(mid, update_data)
                fields_updated += 1
                log.info(f"  [field-fix] ✅ {name}: עודכן {list(update_data.keys())}")
                stats["changes"].append({"action": "fields_updated", "name": name,
                                         "name_he": name_he, "fields": list(update_data.keys())})
        except Exception as e:
            stats["errors"] += 1
            log.error(f"  [field-fix] ❌ {name}: {e}")

    for zoho_id, new_he in to_update_name_he:
        try:
            update_model(zoho_id, {"Car_Model_Name_HE": new_he})
            log.info(f"  [name-fix] ✅ עודכן שם עברי: {new_he}")
        except Exception as e:
            log.error(f"  [name-fix] ❌ שגיאה: {e}")

    log.info("══════════════════════════════════════════════")
    log.info(f"סיכום {mfr_en}: +{stats['created']} נוצרו | "
             f"▲{stats['activated']} הודלקו | ▼{stats['deactivated']} כובו | "
             f"={stats['no_change']} ללא שינוי | ✗{stats['errors']} שגיאות | "
             f"📝{fields_updated} שדות עודכנו")
    log.info("══════════════════════════════════════════════")
    return stats


def _from_approved_source(url: str) -> bool:
    if not url:
        return False
    return (
        "icar.co.il/_media/images/models/bgremoval" in url or
        "auto.co.il" in url or
        "/car-images/" in url
    )
