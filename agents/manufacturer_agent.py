"""
agents/manufacturer_agent.py

סוכן יצרנים:
1. שולף יצרנים מ-Zoho דרך הפונקציה
2. שולף יצרנים פעילים בישראל מ-data.gov.il
3. משווה — מזהה מה השתנה
4. מעדכן ב-Zoho רק מה שצריך
"""

import httpx
import logging
from dataclasses import dataclass

# import יחסי — עובד גם מהתיקייה הראשית וגם מ-agents/
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from zoho_client import get_manufacturers, update_manufacturer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ManufacturerAgent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ────────────────────────────────────────────
# מבנה נתונים
# ────────────────────────────────────────────

@dataclass
class Manufacturer:
    zoho_id:     str
    name_he:     str
    name_en:     str
    active:      bool
    logo_url:    str | None
    description: str | None


def _parse(r: dict) -> Manufacturer:
    return Manufacturer(
        zoho_id=     r["id"],
        name_he=     r.get("Name", ""),
        name_en=     r.get("Car_Manufacturer_Name_EN", ""),
        active=      bool(r.get("Active", False)),
        logo_url=    r.get("Logo_URL"),
        description= r.get("Description"),
    )


# ────────────────────────────────────────────
# מקור אמת — יצרנים פעילים בישראל
# ────────────────────────────────────────────

_israel_cache: set[str] | None = None  # קאש לכל ריצה


def _get_israel_manufacturers() -> set[str]:
    global _israel_cache
    if _israel_cache is not None:
        return _israel_cache

    log.info("שולף יצרנים מ-data.gov.il...")
    manufacturers = set()
    offset = 0

    while True:
        resp = httpx.get(
            "https://data.gov.il/api/3/action/datastore_search",
            params={
                "resource_id": "053cea08-09bc-40ec-8f7a-156f0677aff3",
                "fields":      "tozeret_nm",
                "distinct":    True,
                "limit":       1000,
                "offset":      offset,
            },
            timeout=30,
        )
        resp.raise_for_status()
        records = resp.json().get("result", {}).get("records", [])
        if not records:
            break
        for r in records:
            name = r.get("tozeret_nm", "").strip().upper()
            if name:
                manufacturers.add(name)
        offset += 1000

    log.info(f"נמצאו {len(manufacturers)} יצרנים ברשות הרישוי")
    _israel_cache = manufacturers
    return manufacturers


# ────────────────────────────────────────────
# לוגיקת השוואה
# ────────────────────────────────────────────

def _should_be_active(name_en: str, israel: set[str]) -> bool:
    """בודק אם היצרן קיים בישראל — התאמה ישירה ואחר כך חלקית"""
    n = name_en.strip().upper()
    if n in israel:
        return True
    # מכסה מקרים כמו "VW" ↔ "VOLKSWAGEN"
    return any(n in il or il in n for il in israel)


def _detect_changes(mfr: Manufacturer, should_active: bool) -> dict | None:
    """מחזיר רק את השדות שצריך לעדכן, או None אם הכל תקין"""
    changes = {}

    if mfr.active != should_active:
        changes["Active"] = should_active
        log.info(f"  ✏️  {mfr.name_en}: Active {mfr.active} → {should_active}")

    # נקודת הרחבה עתידית:
    # if not mfr.logo_url:
    #     changes["Logo_URL"] = fetch_logo(mfr.name_en)

    return changes or None


# ────────────────────────────────────────────
# הסוכן הראשי
# ────────────────────────────────────────────

def run() -> dict:
    log.info("══════════════════════════════════════")
    log.info("מתחיל סריקת יצרנים")
    log.info("══════════════════════════════════════")

    # שלב 1 — שליפת נתונים
    zoho_raw      = get_manufacturers()
    manufacturers = [_parse(r) for r in zoho_raw]
    israel        = _get_israel_manufacturers()

    log.info(f"Zoho: {len(manufacturers)} יצרנים | ישראל: {len(israel)} יצרנים")

    # שלב 2 — השוואה ועדכון
    stats = {"total": len(manufacturers), "updated": 0, "no_change": 0, "errors": 0, "updates": []}

    for mfr in manufacturers:
        should_active = _should_be_active(mfr.name_en, israel)
        changes       = _detect_changes(mfr, should_active)

        if changes is None:
            stats["no_change"] += 1
            continue

        try:
            update_manufacturer(mfr.zoho_id, changes)
            stats["updated"] += 1
            stats["updates"].append({"name_en": mfr.name_en, "name_he": mfr.name_he, "changes": changes})
            log.info(f"  ✅ {mfr.name_en} ({mfr.name_he})")
        except Exception as e:
            stats["errors"] += 1
            log.error(f"  ❌ שגיאה — {mfr.name_en}: {e}")

    log.info("══════════════════════════════════════")
    log.info(f"סיכום: {stats['total']} יצרנים | {stats['updated']} עודכנו | "
             f"{stats['no_change']} ללא שינוי | {stats['errors']} שגיאות")
    log.info("══════════════════════════════════════")
    return stats


if __name__ == "__main__":
    results = run()
    if results["updates"]:
        print("\nעודכנו:")
        for u in results["updates"]:
            print(f"  {u['name_en']}: {u['changes']}")
