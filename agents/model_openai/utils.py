"""
agents/model_openai/utils.py
Constants, shared helpers — no business logic.
"""
import json
import logging
import os
import re

from openai import OpenAI

log = logging.getLogger(__name__)

MODEL             = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
MAX_OUTPUT_TOKENS = 16000

CAR_TYPES = {"חשמלי", "היברידי", "בנזין", "דיזל", "פלאג-אין היברידי"}

_CAR_TYPE_ALIASES = {
    "היברידי-נטען":   "פלאג-אין היברידי",
    "היברידי נטען":   "פלאג-אין היברידי",
    "plug-in hybrid": "פלאג-אין היברידי",
    "phev":           "פלאג-אין היברידי",
    "electric":       "חשמלי",
    "ev":             "חשמלי",
    "hybrid":         "היברידי",
    "petrol":         "בנזין",
    "gasoline":       "בנזין",
    "benzin":         "בנזין",
    "diesel":         "דיזל",
}

VALID_CATEGORIES = {
    # body / size
    "SUV", "SUV גדול", "קרוסאובר", "סדאן", "האצ'בק",
    "מיניוואן", "קופה", "פיקאפ", "ספורט", "ליסבק",
    # seating
    "7 מושבים",
    # drivetrain — same values as CAR_TYPES so AI can put them in Category too
    "חשמלי", "היברידי", "פלאג-אין היברידי", "בנזין", "דיזל",
}

_BAD_IMAGE_SIGNALS = (
    "logo", "badge", "emblem", "icon", "mandir", "temple",
    "monument", "chryslerlogo", "ramlogo", "flag", "portrait",
)


def get_client() -> OpenAI:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY לא מוגדר")
    return OpenAI(api_key=key)


def ai_call(client: OpenAI, prompt: str, model: str | None = None) -> str:
    resp = client.responses.create(
        model=model or MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        tools=[{"type": "web_search_preview"}],
        input=[{"role": "user", "content": prompt}],
    )
    for item in resp.output:
        if item.type == "message":
            for block in item.content:
                if block.type == "output_text":
                    return block.text
    return ""


def parse_json(text: str) -> dict:
    from json_repair import repair_json
    for pat in [r"```(?:json)?\s*(\{.*?\})\s*```", r"\{.*\}"]:
        m = re.search(pat, text, re.DOTALL)
        if not m:
            continue
        raw = m.group(1) if m.lastindex else m.group()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return json.loads(repair_json(raw))
            except Exception:
                continue
    return {}


def normalize_car_type(ct: str) -> str:
    if ct in CAR_TYPES:
        return ct
    return _CAR_TYPE_ALIASES.get((ct or "").strip().lower(), ct)


def clean_categories(raw: list) -> list[str]:
    result = []
    for item in (raw or []):
        s = str(item).strip()
        if s in VALID_CATEGORIES:
            result.append(s)
        else:
            log.info(f"  [category] דילג על ערך לא מוכר: '{s}'")
    return result


def needs_image_update(url: str) -> bool:
    if not url:
        return True
    fn = url.lower().split("/")[-1]
    return url.lower().endswith(".svg") or any(s in fn for s in _BAD_IMAGE_SIGNALS)


def from_approved_source(url: str) -> bool:
    if not url:
        return False
    return url.startswith("https://images.gsmdev.co.il/car-images/")


def he_prefix_match(he_name: str, base: str) -> bool:
    """Word-boundary prefix match."""
    return he_name == base or he_name.startswith(base + " ")


def strip_manufacturer_prefix(name: str, mfr_en: str = "", mfr_he: str = "") -> str:
    """Strip manufacturer name prefix from a model name.

    e.g. 'ב.מ.וו X4' → 'X4', 'BMW X4' → 'X4', 'BYD Seal 5' → 'Seal 5'
    Tries: Hebrew name, English name (original case), then case-insensitive English.
    """
    if not name:
        return name
    for pfx in filter(None, [
        (mfr_he + " ") if mfr_he else None,
        (mfr_en + " ") if mfr_en else None,
        (mfr_en.upper() + " ") if mfr_en else None,
    ]):
        if name.startswith(pfx):
            return name[len(pfx):]
    if mfr_en:
        lower_pfx = mfr_en.lower() + " "
        if name.lower().startswith(lower_pfx):
            return name[len(lower_pfx):]
    return name


def validate_record(record: dict) -> list[str]:
    issues = []
    if not record.get("Name"):
        issues.append("Name ריק")
    elif any("֐" <= c <= "׿" for c in record.get("Name", "")):
        issues.append("Name מכיל עברית")
    if not record.get("Car_Model_Name_HE"):
        issues.append("Car_Model_Name_HE ריק")
    if not record.get("Description"):
        issues.append("Description ריק")
    if record.get("Car_Type") not in CAR_TYPES:
        issues.append(f"Car_Type לא תקין: {record.get('Car_Type')}")
    yr = str(record.get("Year_From", ""))
    if not re.fullmatch(r"\d{4}", yr):
        issues.append(f"Year_From לא תקין: {yr}")
    return issues
