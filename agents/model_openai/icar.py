"""
agents/model_openai/icar.py
Pulse 1: icar.co.il direct scrape (primary) + AI discovery fallback.
"""
import logging
import re
from urllib.parse import quote, unquote

import httpx
from openai import OpenAI

from .utils import ai_call, parse_json, strip_manufacturer_prefix

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CarAgentBot/1.0)"}


def _scrape_icar(mfr_he: str, mfr_en: str) -> list[dict]:
    """Scrapes icar.co.il without AI. Returns active models as [{name_he, icar_slug, image_url}]."""

    def try_slug(slug: str) -> list[dict]:
        url = "https://www.icar.co.il/" + quote(slug, safe="") + "/"
        try:
            r = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=12)
        except Exception as e:
            log.warning(f"[icar] שגיאת חיבור '{slug}': {e}")
            return []
        if r.status_code != 200:
            log.debug(f"[icar] '{slug}' → {r.status_code}")
            return []

        html = r.text
        prefix = "/" + slug + "/"
        models: dict[str, dict] = {}

        for raw_link in re.findall(r'href=["\'](/[^"\'<>\s?#]+)', html):
            link = unquote(raw_link)
            if not link.startswith(prefix):
                continue
            parts = link.strip("/").split("/")
            if len(parts) < 2:
                continue
            model_slug = parts[1]
            if not model_slug or model_slug == slug:
                continue
            if model_slug not in models:
                name_he = strip_manufacturer_prefix(
                    model_slug.replace("_", " "), mfr_en, mfr_he
                )
                models[model_slug] = {
                    "name_he":   name_he,
                    "icar_slug": model_slug,
                    "has_new":   False,
                    "image_url": "",
                }
            if len(parts) == 3 and "חדש" in parts[2]:
                models[model_slug]["has_new"] = True

        # icar encodes only apostrophes (' → %27) but keeps Hebrew chars raw.
        # Match both the literal slug and the partially-encoded form.
        slug_apos = slug.replace("'", "%27")  # "צ'רי" → "צ%27רי"
        if slug_apos == slug:
            mfr_alt = re.escape("/" + slug) + r"/"
        else:
            mfr_alt = r"(?:" + re.escape("/" + slug) + r"|" + re.escape("/" + slug_apos) + r")/"

        block_pattern = re.compile(
            r'href=["\']' + mfr_alt
            + r'([^"\'<>\s?#/]+)/[^"\'<>\s?#]*חדש[^"\'<>\s?#]*/'
            + r'["\'].*?'
            + r'src=["\'](https://www\.icar\.co\.il/_media/images/models/bgremoval/[^"\'<>\s]+)["\']',
            re.DOTALL,
        )
        for m in block_pattern.finditer(html):
            model_slug = unquote(m.group(1))  # decode %27 → ' etc.
            img_url    = m.group(2)
            if model_slug in models:
                models[model_slug]["image_url"] = img_url

        active = [v for v in models.values() if v["has_new"]]
        if active:
            log.info(f"[icar] '{slug}': {len(active)} דגמים פעילים")
        return active

    for slug in dict.fromkeys([mfr_he, mfr_en.upper(), mfr_en]):
        if not slug:
            continue
        result = try_slug(slug)
        if result:
            return result

    log.warning(f"[icar] לא נמצאו דגמים עבור {mfr_en}/{mfr_he}")
    return []


def _ai_discover_models(client: OpenAI, mfr_en: str, mfr_he: str) -> list[dict]:
    """AI fallback when icar returns nothing."""
    prompt = f"""You are verifying which {mfr_en} ({mfr_he}) car models are sold NEW in Israel in 2025-2026.

MANDATORY SEARCH STEPS — complete ALL before answering:
1. Search: "{mfr_en} יבואן רשמי ישראל 2025" — find the official Israeli importer
2. If importer found: fetch their website, list exact models with current 2025-2026 pricing
3. Search: site:auto.co.il {mfr_en} — check which models appear on auto.co.il
4. Search: site:icar.co.il {mfr_en} — check if icar.co.il lists any current models

STRICT INCLUSION RULES — a model is included ONLY if:
✅ Official Israeli importer actively sells it with 2025-2026 pricing
✅ Listed on auto.co.il or icar.co.il as a current new-car offering

STRICT EXCLUSION RULES — do NOT include if:
❌ Only found in yad2 used-car listings
❌ Only found in news articles from before 2024
❌ Only in your training data with no current web evidence
❌ Manufacturer has no official Israeli importer → return empty list
❌ When in doubt → do NOT include

Model name only — do NOT include the manufacturer brand: "Pacifica" not "Chrysler Pacifica", "300C" not "Chrysler 300C".

Return ONLY valid JSON:
{{
  "models": [
    {{"name_en": "Pacifica", "name_he": "פסיפיקה", "source": "importer-url.co.il"}}
  ]
}}
If none found: {{"models": []}}"""

    text = ai_call(client, prompt)
    data = parse_json(text)
    models = data.get("models", [])
    for m in models:
        for field in ("name_en", "name_he"):
            val = m.get(field, "")
            if val:
                m[field] = strip_manufacturer_prefix(val, mfr_en, mfr_he)
    log.info(f"[pulse-1-ai] דגמים מ-AI: {[m.get('name_en','') for m in models]}")
    return models


def _he_prefix_match(he_name: str, base: str) -> bool:
    return he_name == base or he_name.startswith(base + " ")


def get_israel_models(
    client: OpenAI, mfr_en: str, mfr_he: str, existing: list[dict] | None = None
) -> tuple[list[dict], bool]:
    """
    Returns (models, icar_used).
    icar_used=True  → icar is authoritative, no confirmation needed.
    icar_used=False → AI source, confirmation required.
    """
    existing = existing or []
    existing_by_he = {(m.get("Car_Model_Name_HE") or "").upper(): m for m in existing if m.get("Car_Model_Name_HE")}
    existing_by_en = {(m.get("Name") or "").upper(): m for m in existing if m.get("Name")}

    icar_models = _scrape_icar(mfr_he, mfr_en)

    if icar_models:
        result = []
        for im in icar_models:
            name_he = im["name_he"]
            existing_rec = existing_by_he.get(name_he.upper())
            if not existing_rec:
                parts = name_he.split(" ", 1)
                if len(parts) == 2:
                    existing_rec = existing_by_he.get(parts[1].upper())
            if not existing_rec:
                parts = name_he.split(" ", 1)
                if len(parts) == 2:
                    existing_rec = existing_by_en.get(parts[1].upper())
            name_en = existing_rec.get("Name", "") if existing_rec else ""
            result.append({"name_he": name_he, "name_en": name_en,
                           "image_url": im.get("image_url", ""), "from_icar": True})
        log.info(f"[pulse-1] icar: {[m['name_he'] for m in result]}")
        for m in result:
            if m.get("image_url"):
                log.info(f"  [icar-img] {m['name_he']}: {m['image_url']}")
        return result, True

    log.warning("[pulse-1] icar ריק — עובר ל-AI fallback")
    ai_models = _ai_discover_models(client, mfr_en, mfr_he)
    result = []
    for m in ai_models:
        name_en = m.get("name_en", "")
        name_he = m.get("name_he", "")
        if name_en.upper() in existing_by_en:
            name_en = existing_by_en[name_en.upper()].get("Name", name_en)
        result.append({"name_he": name_he, "name_en": name_en, "from_icar": False})
    return result, False
