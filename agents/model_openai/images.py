"""
agents/model_openai/images.py
Pulse 3: car image lookup (icar bgremoval primary, auto.co.il secondary).
"""
import logging
import re

import httpx
from openai import OpenAI

from .utils import ai_call

log = logging.getLogger(__name__)

_BAD_FNAMES = ("logo", "badge", "emblem", "icon", "flag", "mandir", "temple")

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.auto.co.il/",
}


def verify_url(url: str) -> bool:
    if not url:
        return False
    if url.lower().endswith(".svg"):
        log.info(f"    verify {url[:70]} → SVG rejected ✗")
        return False
    fname = url.split("/")[-1].lower()
    if any(s in fname for s in _BAD_FNAMES):
        log.info(f"    verify {url[:70]} → logo/non-car rejected ✗")
        return False
    # Wikimedia originals — trusted without HEAD
    if re.match(
        r"https://upload\.wikimedia\.org/wikipedia/commons/[0-9a-f]/[0-9a-f]{2}/.+\.(jpg|jpeg|png|webp)$",
        url, re.IGNORECASE,
    ):
        log.info(f"    verify {url[:70]} → wikimedia commons (trusted) ✓")
        return True
    try:
        headers = {"User-Agent": _BROWSER_HEADERS["User-Agent"]}
        if "auto.co.il" in url:
            headers["Referer"] = "https://www.auto.co.il/"
        r = httpx.head(url, timeout=8, follow_redirects=True, headers=headers)
        ct = r.headers.get("content-type", "")
        ok = (r.status_code == 200 and "image" in ct) or \
             (r.status_code == 429 and any(url.lower().endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")))
        log.info(f"    verify {url[:70]} → {r.status_code} {'✓' if ok else '✗'}")
        return ok
    except Exception as e:
        log.warning(f"    verify failed: {e}")
        return False


def scrape_auto_co_il_image(mfr_en: str, name_en: str) -> str:
    """Direct scrape of auto.co.il for a model image. Returns empty string if not found."""

    def slugify(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9\s-]", "", s)
        return re.sub(r"[\s]+", "-", s).strip("-")

    mfr_slug = slugify(mfr_en)
    name_clean = name_en[len(mfr_en):].strip() if name_en.upper().startswith(mfr_en.upper()) else name_en
    model_slug = slugify(name_clean)
    page_url = f"https://www.auto.co.il/cars/{mfr_slug}/{model_slug}/"
    log.info(f"  [auto.co.il] fetching {page_url}")

    try:
        r = httpx.get(page_url, timeout=10, follow_redirects=True, headers=_BROWSER_HEADERS)
        if r.status_code != 200:
            log.info(f"  [auto.co.il] {r.status_code} — not found")
            return ""
        imgs = re.findall(
            r'https://static\.auto\.co\.il/media/[A-Za-z][A-Za-z0-9]+/[^"\'<>\s]+\.(?:jpg|jpeg|png|webp)',
            r.text,
        )
        if not imgs:
            return ""
        seen, unique = set(), []
        for u in imgs:
            base = u.split("?")[0]
            if base not in seen:
                seen.add(base)
                unique.append(base)
        best = next((u for u in unique if "logo" not in u.lower() and "icon" not in u.lower()), unique[0])
        log.info(f"  [auto.co.il] ✓ {best[:90]}")
        return best
    except Exception as e:
        log.warning(f"  [auto.co.il] scrape error: {e}")
        return ""


def _download_and_store(url: str, filename: str) -> str:
    """Downloads image from URL, saves to IMAGES_DIR, returns permanent local URL."""
    import os
    images_dir  = os.environ.get("IMAGES_DIR", "/var/www/car-images")
    images_base = os.environ.get("IMAGES_BASE_URL", "http://46.101.128.223/car-images")
    os.makedirs(images_dir, exist_ok=True)
    path = os.path.join(images_dir, filename)
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    with open(path, "wb") as f:
        f.write(resp.content)
    return f"{images_base}/{filename}"


def get_image_carimagesapi(mfr_en: str, name_en: str) -> str:
    """Fallback: carimagesapi.com — downloads image locally, returns permanent URL."""
    import os
    api_key    = os.environ.get("CARIMAGES_API_KEY", "ci_2b68a56ce8252d49f71206fbf877e79d3766c223c2832b862af906ad")
    api_secret = os.environ.get("CARIMAGES_API_SECRET", "49e9715026231b80e627e20456eecb402bb7a7278244753ee7ba476e53f4be92")

    # Strip brand prefix: "BYD Seal" → model="Seal"
    model_name = name_en
    if model_name.upper().startswith(mfr_en.upper() + " "):
        model_name = model_name[len(mfr_en) + 1:]

    try:
        resp = httpx.get(
            "https://carimagesapi.com/api/v1/signed-url",
            params={
                "api_key":    api_key,
                "api_secret": api_secret,
                "make":       mfr_en,
                "model":      model_name,
                "year":       2026,
                "format":     "png",
                "width":      800,
            },
            timeout=10,
        )
        resp.raise_for_status()
        signed_url = resp.json().get("url", "")
        if not signed_url:
            return ""

        # Detect placeholder ("covered car") before downloading
        head = httpx.head(signed_url, timeout=8, follow_redirects=True)
        etag = head.headers.get("etag", "")
        size = int(head.headers.get("content-length", 0))
        if etag == '"69b15582-1ad3c"' or (0 < size < 120_000):
            log.warning(f"  [carimagesapi] placeholder detected for {name_en} — מדלג")
            return ""

        slug     = re.sub(r"[^a-z0-9]+", "-", f"{mfr_en}-{model_name}".lower()).strip("-")
        filename = f"{slug}.png"
        local_url = _download_and_store(signed_url, filename)
        log.info(f"  [carimagesapi] ✓ {name_en} → {filename}")
        return local_url

    except Exception as e:
        log.warning(f"  [carimagesapi] error for {name_en}: {e}")
        return ""


def get_image(client: OpenAI, mfr_en: str, name_en: str, name_he: str) -> str:
    """
    Fallback image when icar has no bgremoval image.
    1. Direct scrape of auto.co.il.
    2. AI-assisted search on auto.co.il.
    3. carimagesapi.com (signed URL, temporary).
    Returns empty string if nothing found.
    """
    label = name_en or name_he

    url = scrape_auto_co_il_image(mfr_en, name_en)
    if url and verify_url(url):
        log.info(f"  [image] {label}: auto.co.il ✓ (direct)")
        return url

    log.info(f"  [image] {label}: direct scrape נכשל — מנסה AI search...")
    prompt = f"""Find a direct car image URL for the {mfr_en} {name_en} from auto.co.il only.

Search: site:auto.co.il {mfr_en} {name_en}
Also try searching in Hebrew: site:auto.co.il {name_he}
Fetch the car page on auto.co.il and extract an image URL from static.auto.co.il/media/

Requirements:
- URL must be from static.auto.co.il/media/ and end in .jpg / .jpeg / .png / .webp
- Must be a direct image file URL (not a webpage)
- Must show the {mfr_en} {name_en} — not a logo, not a different model

Return ONLY the image URL on a single line, or NONE if not found.
No explanation, no markdown."""

    text = ai_call(client, prompt).strip()
    if not text or text.upper().startswith("NONE"):
        log.info(f"  [image] {label}: auto.co.il לא מצא — מנסה carimagesapi...")
        url = get_image_carimagesapi(mfr_en, name_en)
        if url:
            return url
        log.warning(f"  [image] {label}: לא נמצאה תמונה — משאיר ריק")
        return ""

    url_match = re.search(
        r'https://static\.auto\.co\.il/media/[A-Za-z][A-Za-z0-9]+/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
        text, re.IGNORECASE,
    )
    if not url_match:
        log.info(f"  [image] {label}: auto.co.il (AI) לא מצא — מנסה carimagesapi...")
        url = get_image_carimagesapi(mfr_en, name_en)
        if url:
            return url
        log.warning(f"  [image] {label}: לא נמצאה תמונה — משאיר ריק")
        return ""

    url = url_match.group(0).split("?")[0]
    if verify_url(url):
        log.info(f"  [image] {label}: auto.co.il ✓ (AI) {url[:80]}")
        return url

    log.info(f"  [image] {label}: auto.co.il URL לא עבר אימות — מנסה carimagesapi...")
    url = get_image_carimagesapi(mfr_en, name_en)
    if url:
        return url

    log.warning(f"  [image] {label}: לא נמצאה תמונה — משאיר ריק")
    return ""
