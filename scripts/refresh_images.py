"""
scripts/refresh_images.py

One-time migration: fills missing images on all active Zoho models
using carimagesapi. Skips models that already have an approved image.

Usage:
    cd /opt/car-agent && venv/bin/python scripts/refresh_images.py [--dry-run]
"""
import logging
import os
import sys
import argparse

sys.path.insert(0, "/opt/car-agent")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [refresh] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _already_local(url: str) -> bool:
    """רק תמונות על הדומיין הרשמי שלנו עם HTTPS — לא מחליפים."""
    return bool(url) and url.startswith("https://images.gsmdev.co.il/car-images/")


def run(dry_run: bool = False):
    from zoho_client import _call, _MODEL_URL, _MANUFACTURER_URL, update_model
    from agents.model_openai.images import get_image_carimagesapi

    log.info("שולף יצרנים מ-Zoho...")
    mfr_result = _call(_MANUFACTURER_URL, {"action": "get"})
    manufacturers = mfr_result.get("message") or mfr_result.get("data") or []
    log.info(f"{len(manufacturers)} יצרנים")

    stats = {"checked": 0, "skipped": 0, "updated": 0, "failed": 0, "no_image": 0}

    for mfr in manufacturers:
        mfr_id   = str(mfr.get("id", ""))
        mfr_en   = mfr.get("Car_Manufacturer_Name_EN") or mfr.get("Name", "")
        if not mfr_en:
            continue

        models_result = _call(_MODEL_URL, {"action": "get", "manufacturer_id": int(mfr_id)})
        models = models_result.get("message") or models_result.get("data") or []
        if not isinstance(models, list):
            continue

        active_models = [m for m in models if m.get("Active")]
        log.info(f"\n{mfr_en}: {len(active_models)} דגמים פעילים")

        for m in active_models:
            stats["checked"] += 1
            model_id = str(m.get("id", ""))
            name_en  = m.get("Name", "")
            current  = m.get("Model_Image_URL", "")

            if _already_local(current):
                log.info(f"  ⏭  {name_en} — כבר על השרת, מדלג")
                stats["skipped"] += 1
                continue

            log.info(f"  🔍 {name_en} — מחפש תמונה...")
            url = get_image_carimagesapi(mfr_en, name_en)

            if not url:
                log.warning(f"  ❌ {name_en} — לא נמצאה תמונה")
                stats["no_image"] += 1
                if not dry_run:
                    try:
                        update_model(model_id, {"System_Alert": "לא נמצאה תמונה לדגם"})
                    except Exception as e:
                        log.error(f"  ❌ System_Alert {name_en}: {e}")
                continue

            if dry_run:
                log.info(f"  [dry-run] היה מעדכן: {name_en} → {url}")
                stats["updated"] += 1
                continue

            try:
                update_model(model_id, {"Model_Image_URL": url})
                log.info(f"  ✅ {name_en} → {url.split('/')[-1]}")
                stats["updated"] += 1
            except Exception as e:
                log.error(f"  ❌ {name_en} שגיאת עדכון: {e}")
                stats["failed"] += 1

    log.info(f"""
{'═'*50}
סיכום:
  נבדקו:      {stats['checked']}
  דולגו:      {stats['skipped']} (תמונה קיימת)
  עודכנו:     {stats['updated']}
  ללא תמונה:  {stats['no_image']}
  שגיאות:     {stats['failed']}
{'═'*50}
    """.strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="הצג בלי לכתוב ל-Zoho")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
