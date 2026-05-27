"""
agents/scrapers/icar.py
Direct HTTP scraper for icar.co.il — no AI required.

Provides:
  get_active_models(mfr_en, mfr_he) → list of model dicts
  get_model_specs(mfr_slug, model_slug, name_he) → dict
"""
import logging
import re
from urllib.parse import unquote

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CarAgentBot/1.0)"}
_TIMEOUT = 12

# ── Car type: ordered most-specific → least-specific ─────────────────────────
# Only used when car type cannot be inferred from the model name.
# No default — if nothing matches, car_type is left out of the result.
_TYPE_PATTERNS = [
    (re.compile(r'פלאג.אין|plug.in|phev|נטענ', re.I),          "פלאג-אין היברידי"),
    (re.compile(r'הנעה חשמלית|רכב חשמלי|\bEV\b|חשמלית\b|חשמלי\b', re.I), "חשמלי"),
    (re.compile(r'היברידי',                                      re.I), "היברידי"),
    (re.compile(r'דיזל',                                         re.I), "דיזל"),
    (re.compile(r'בנזין',                                        re.I), "בנזין"),
]

# ── icar sub-category → our category value ───────────────────────────────────
# Parsed from the structured paragraph: "קטגוריה: X תתי קטגוריות: Y"
# Requires "תתי קטגוריות" marker to avoid false matches on description text.
# Ordered most-specific first; matched against the *full* structured string.
_ICAR_CAT_MAP = [
    (re.compile(r'פנאי.שטח.+(גדול|מלא)',       re.I), "SUV גדול"),
    (re.compile(r'פנאי.שטח.+(בינוני)',          re.I), "SUV"),
    (re.compile(r'פנאי.שטח.+קטן',              re.I), "קרוסאובר"),
    (re.compile(r'7 מושבים|שבעה מושבים',        re.I), "7 מושבים"),
    (re.compile(r'קרוסאובר',                    re.I), "קרוסאובר"),
    (re.compile(r'פנאי.שטח|רכב שטח',           re.I), "SUV"),
    (re.compile(r"האצ'בק|hatchback",            re.I), "האצ'בק"),
    (re.compile(r'סופר.מיני|\bמיני\b',          re.I), "האצ'בק"),
    (re.compile(r'ליסבק',                       re.I), "ליסבק"),
    (re.compile(r'מיניוואן',                    re.I), "מיניוואן"),
    (re.compile(r'טנדר|פיקאפ',                 re.I), "פיקאפ"),
    (re.compile(r'קופה',                        re.I), "קופה"),
    (re.compile(r'ספורט',                       re.I), "ספורט"),
    (re.compile(r'מנהלים|sedan|סדאן',           re.I), "סדאן"),
    (re.compile(r'משפחתי',                      re.I), "סדאן"),
]

# ── Fallback text-based category patterns ─────────────────────────────────────
# Used when icar's structured category paragraph is absent.
_CAT_PATTERNS = [
    (re.compile(r'SUV גדול|ג\'יפ גדול',              re.I), "SUV גדול"),
    (re.compile(r'\bSUV\b',                           re.I), "SUV"),
    (re.compile(r'7 מושבים|שבעה מושבים',             re.I), "7 מושבים"),
    (re.compile(r'קרוסאובר',                          re.I), "קרוסאובר"),
    (re.compile(r"ג'יפון קטן|פנאי קומפקטי",          re.I), "קרוסאובר"),
    (re.compile(r"רכב פנאי|ג'יפ|ג'יפון|רכב שטח",    re.I), "SUV"),
    (re.compile(r"האצ'בק",                            re.I), "האצ'בק"),
    (re.compile(r'מיניוואן',                          re.I), "מיניוואן"),
    (re.compile(r'ליסבק',                             re.I), "ליסבק"),
    (re.compile(r'פיקאפ|טנדר',                       re.I), "פיקאפ"),
    (re.compile(r'קופה',                              re.I), "קופה"),
    (re.compile(r'ספורט',                             re.I), "ספורט"),
    (re.compile(r'סדאן|משפחתית',                     re.I), "סדאן"),
]


def _get(url: str) -> httpx.Response | None:
    try:
        r = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=_TIMEOUT)
        return r if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"[icar] HTTP error {url}: {e}")
        return None


def _find_mfr_slug(mfr_he: str, mfr_en: str) -> tuple[str, httpx.Response] | tuple[None, None]:
    """Try Hebrew → UPPER English → mixed English to find the manufacturer page."""
    for slug in dict.fromkeys(filter(None, [mfr_he, mfr_en.upper(), mfr_en])):
        r = _get(f"https://www.icar.co.il/{slug}/")
        if r:
            return slug, r
    log.warning(f"[icar] manufacturer not found: {mfr_en} / {mfr_he}")
    return None, None


def _content_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove sidebar (home_left) and non-content tags only."""
    for tag in soup.find_all("div", class_="home_left"):
        tag.decompose()
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    return soup


def _icar_category(soup: BeautifulSoup) -> str:
    """
    Parse icar's structured category paragraph:
      "קטגוריה: רכב שטח תתי קטגוריות: פנאי-שטח בגודל מלא"
    Requires "תתי קטגוריות" to avoid false matches on description text.
    Returns our category string, or "" if not found.
    """
    for p in soup.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if "תתי קטגוריות" in txt:
            for pattern, category in _ICAR_CAT_MAP:
                if pattern.search(txt):
                    return category
            break
    return ""


# ── Public: discover active models ───────────────────────────────────────────

def get_active_models(mfr_en: str, mfr_he: str) -> list[dict]:
    """
    Scrape icar manufacturer page — return only models with a חדש subpage (active).
    Each result: {name_he, mfr_slug, model_slug, image_url}
    """
    mfr_slug, r = _find_mfr_slug(mfr_he, mfr_en)
    if not r:
        return []

    html   = r.text
    prefix = f"/{mfr_slug}/"
    all_links = [unquote(l) for l in re.findall(r'href=["\']([^"\'<>\s?#]+)["\']', html)]

    active: dict[str, dict] = {}
    for link in all_links:
        if not link.startswith(prefix):
            continue
        parts = link.strip("/").split("/")
        if len(parts) == 3 and "חדש" in parts[2]:
            model_slug = parts[1]
            name_he = model_slug
            for pfx in [mfr_slug + "_", mfr_en.upper() + "_", mfr_en + "_"]:
                if name_he.upper().startswith(pfx.upper()):
                    name_he = name_he[len(pfx):]
                    break
            name_he = name_he.replace("_", " ")
            active[model_slug] = {
                "name_he":    name_he,
                "mfr_slug":   mfr_slug,
                "model_slug": model_slug,
                "image_url":  "",
            }

    # Extract bgremoval images
    img_pattern = re.compile(
        r'href=["\']/' + re.escape(mfr_slug) + r'/([^"\'<>\s?#/]+)/[^"\'<>\s?#]*חדש[^"\'<>\s?#]*/["\']'
        r'.*?src=["\'](https://www\.icar\.co\.il/_media/images/models/bgremoval/[^"\'<>\s]+)["\']',
        re.DOTALL,
    )
    for m in img_pattern.finditer(html):
        slug = unquote(m.group(1))
        if slug in active:
            active[slug]["image_url"] = m.group(2)

    result = list(active.values())
    log.info(f"[icar] {mfr_en}: {len(result)} active models")
    return result


# ── Public: get model specs ───────────────────────────────────────────────────

def get_model_specs(mfr_slug: str, model_slug: str, name_he: str = "") -> dict:
    """
    Scrape the icar model page for year_from, car_type, category.
    Returns dict — only includes keys where data was actually found.
    car_type is never guessed; left out if unclear.
    """
    url = f"https://www.icar.co.il/{mfr_slug}/{model_slug}/"
    r = _get(url)
    if not r:
        log.warning(f"[icar-specs] not found: {url}")
        return {}

    soup   = _content_soup(BeautifulSoup(r.text, "html.parser"))
    result = {}

    # ── Year_From ──────────────────────────────────────────────────────────────
    # H2s on icar model pages: "BYD דולפין ‏  2023-2025"
    # Take max of generation start years = most recent generation
    h2_years = []
    for h2 in soup.find_all("h2"):
        years = re.findall(r'\b(20[12]\d)\b', h2.get_text(strip=True))
        if years:
            h2_years.append(int(min(years)))
    if h2_years:
        result["year_from"] = max(h2_years)

    # ── Car_Type (drivetrain) ──────────────────────────────────────────────────
    # 1. Model name / slug — most reliable signal
    name_check = (name_he + " " + model_slug).lower()
    if re.search(r'\bev\b|חשמלי', name_check, re.I):
        result["car_type"] = "חשמלי"
    elif re.search(r'פלאג.אין|phev|נטען', name_check, re.I):
        result["car_type"] = "פלאג-אין היברידי"
    elif re.search(r'היברידי|הייבריד|hybrid', name_check, re.I):
        result["car_type"] = "היברידי"
    elif re.search(r'דיזל|diesel', name_check, re.I):
        result["car_type"] = "דיזל"
    else:
        # 2. Description text — only if explicit, no defaults
        desc_div = soup.find("div", class_="desktop_text")
        if desc_div:
            desc_text = desc_div.get_text(" ", strip=True)
        else:
            paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
            desc_text = " ".join(paras[:3])
        for pattern, car_type in _TYPE_PATTERNS:
            if pattern.search(desc_text):
                result["car_type"] = car_type
                break
        # No default — if drivetrain is unclear, omit car_type

    # ── Category ───────────────────────────────────────────────────────────────
    # 1. icar's structured category paragraph (most reliable)
    cat = _icar_category(soup)
    if cat:
        result["category"] = cat
    else:
        # 2. Fallback: text patterns on description div or first paragraphs
        desc_div = soup.find("div", class_="desktop_text")
        if desc_div:
            cat_text = desc_div.get_text(" ", strip=True)
        else:
            paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
            cat_text = " ".join(paras[:4])
        for pattern, category in _CAT_PATTERNS:
            if pattern.search(cat_text):
                result["category"] = category
                break

    log.debug(f"[icar-specs] {model_slug}: {result}")
    return result
