"""
agents/model_openai/images.py
Pulse 3: car image lookup (icar bgremoval primary, auto.co.il secondary).
"""
import hashlib
import logging
import os
import re

import httpx
from openai import OpenAI

from .utils import ai_call

log = logging.getLogger(__name__)

_BAD_FNAMES = ("logo", "badge", "emblem", "icon", "flag", "mandir", "temple")

# carimagesapi serves a generic "covered car" PLACEHOLDER when it has no real image for a
# model (its HEAD is unsupported → 404, so the placeholder must be detected on the downloaded
# bytes). Real car renders are ~300KB+; the placeholder is exactly this md5 / ~110KB.
_PLACEHOLDER_MD5 = {"c27d4340d96194bb625ef85fd6c2c4aa"}
_CARIMAGES_MIN_BYTES = 120_000

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


def _store_bytes(content: bytes, filename: str) -> str:
    """Saves image bytes to IMAGES_DIR, returns permanent local URL."""
    images_dir  = os.environ.get("IMAGES_DIR", "/var/www/car-images")
    images_base = os.environ.get("IMAGES_BASE_URL", "https://images.gsmdev.co.il/car-images")
    os.makedirs(images_dir, exist_ok=True)
    with open(os.path.join(images_dir, filename), "wb") as f:
        f.write(content)
    return f"{images_base}/{filename}"


def _download_and_store(url: str, filename: str) -> str:
    """Downloads image from URL, saves to IMAGES_DIR, returns permanent local URL."""
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    return _store_bytes(resp.content, filename)


def _is_carimages_placeholder(content: bytes) -> bool:
    """carimagesapi's generic 'covered car' image (returned when it has no real photo)."""
    return (len(content) < _CARIMAGES_MIN_BYTES
            or hashlib.md5(content).hexdigest() in _PLACEHOLDER_MD5)


def is_stored_placeholder(image_url: str) -> bool:
    """True if an already-stored local image is the carimagesapi placeholder — heals bad
    images saved before placeholder detection was fixed. Matches the exact placeholder md5
    only (NOT the size heuristic, so genuine small icar images are not flagged)."""
    base = os.environ.get("IMAGES_BASE_URL", "https://images.gsmdev.co.il/car-images")
    if not image_url or not image_url.startswith(base):
        return False
    path = os.path.join(os.environ.get("IMAGES_DIR", "/var/www/car-images"),
                        image_url.rsplit("/", 1)[-1])
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest() in _PLACEHOLDER_MD5
    except OSError:
        return False


def get_image_carimagesapi(mfr_en: str, name_en: str) -> str:
    """Primary image: carimagesapi.com. Returns a permanent local URL, or '' when
    carimagesapi has NO real image for the model (serves a placeholder) — in which case
    the caller should fall back to icar's image."""
    api_key    = os.environ.get("CARIMAGES_API_KEY")
    api_secret = os.environ.get("CARIMAGES_API_SECRET")
    if not api_key or not api_secret:
        log.warning("  [carimagesapi] CARIMAGES_API_KEY/SECRET לא מוגדרים — מדלג")
        return ""

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

        # HEAD is unsupported by the CDN (always 404) — download and inspect the actual
        # bytes to tell a real render from the 'covered car' placeholder.
        img = httpx.get(signed_url, timeout=20, follow_redirects=True)
        if img.status_code != 200 or "image" not in img.headers.get("content-type", ""):
            log.info(f"  [carimagesapi] {name_en}: {img.status_code} — אין תמונה")
            return ""
        if _is_carimages_placeholder(img.content):
            log.warning(f"  [carimagesapi] placeholder ({len(img.content)}B) — אין תמונה אמיתית ל-{name_en}")
            return ""

        slug     = re.sub(r"[^a-z0-9]+", "-", f"{mfr_en}-{model_name}".lower()).strip("-")
        filename = f"{slug}.png"
        local_url = _store_bytes(img.content, filename)
        log.info(f"  [carimagesapi] ✓ {name_en} → {filename}")
        return local_url

    except Exception as e:
        log.warning(f"  [carimagesapi] error for {name_en}: {e}")
        return ""


def get_image_icar(mfr_en: str, name_en: str, icar_url: str) -> str:
    """Fallback image: icar's bgremoval model image. Used only when carimagesapi has no
    real image. Downloads + stores locally for a permanent URL. '' if unavailable."""
    if not icar_url:
        return ""
    try:
        r = httpx.get(icar_url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": _BROWSER_HEADERS["User-Agent"],
                               "Referer": "https://www.icar.co.il/"})
        if r.status_code != 200 or "image" not in r.headers.get("content-type", ""):
            log.info(f"  [icar-img] {name_en}: {r.status_code} — אין תמונה ב-icar")
            return ""
        fname = (icar_url.split("?")[0].split("/")[-1]).lower()
        if any(s in fname for s in _BAD_FNAMES):
            return ""
        ext  = os.path.splitext(fname)[1].lower()
        ext  = ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
        slug = re.sub(r"[^a-z0-9]+", "-", f"{mfr_en}-{name_en}".lower()).strip("-")
        local_url = _store_bytes(r.content, f"{slug}-icar{ext}")
        log.info(f"  [icar-img] ✓ {name_en} → {slug}-icar{ext}")
        return local_url
    except Exception as e:
        log.warning(f"  [icar-img] error for {name_en}: {e}")
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
