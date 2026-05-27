"""
agents/image_agent.py

סוכן תמונות ייעודי — claude-opus-4-7 עם web_search + web_fetch.
משימה יחידה: מצא URL ישיר של תמונת רכב עם רקע לבן/שקוף.
"""

import logging
import os
import re
import sys

import anthropic
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

log = logging.getLogger(__name__)

MODEL            = "claude-opus-4-7"
MAX_TOKENS       = 4096
MAX_CONTINUATIONS = 6

_BAD_SIGNALS = (
    "logo", "badge", "emblem", "icon", "mandir", "temple",
    "monument", "chryslerlogo", "flag", "portrait", "coat_of_arms",
)


def _get_client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY חסר")
    return anthropic.Anthropic(api_key=key)


def _run(client: anthropic.Anthropic, prompt: str) -> str:
    """מריץ קלוד עם web_search + web_fetch ומטפל ב-pause_turn."""
    messages = [{"role": "user", "content": prompt}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209",  "name": "web_fetch"},
    ]
    kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=tools,
        messages=messages,
    )
    resp = client.messages.create(**kwargs)
    for _ in range(MAX_CONTINUATIONS):
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
        kwargs["messages"] = messages
        resp = client.messages.create(**kwargs)
    return next((b.text for b in resp.content if b.type == "text"), "")


def _verify(url: str) -> bool:
    """בודק שה-URL נגיש, מחזיר תמונה, ואינו לוגו/מבנה."""
    if not url:
        return False
    if url.lower().endswith(".svg"):
        return False
    fn = url.lower().split("/")[-1]
    if any(s in fn for s in _BAD_SIGNALS):
        return False
    try:
        r = httpx.head(
            url, timeout=8, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CarAgentBot/1.0)"},
        )
        ct = r.headers.get("content-type", "")
        return (
            (r.status_code == 200 and "image" in ct) or
            (r.status_code == 429 and any(url.lower().endswith(e)
                                          for e in (".jpg", ".jpeg", ".png", ".webp")))
        )
    except Exception:
        return False


def find(mfr_en: str, name_en: str, name_he: str = "") -> str:
    """
    מחפש URL ישיר של תמונת רכב.
    מחזיר URL מאומת, או מחרוזת ריקה אם לא נמצא.
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        log.warning(f"[image_agent] {e}")
        return ""

    mfr_slug   = mfr_en.lower().replace(" ", "-")
    model_slug = name_en.lower().replace(" ", "-")

    prompt = f"""You are an expert at finding official car press images for an automotive database.

YOUR ONLY TASK: Find a working, direct image URL for the {mfr_en} {name_en} ({name_he}).

STRICT REQUIREMENTS for the image:
- Must show the {mfr_en} {name_en} specifically — NOT a different model or variant
- Must have WHITE or TRANSPARENT/STUDIO background (no street, no scenery, no people)
- URL must end in .jpg / .jpeg / .png / .webp  (NEVER .svg)
- Must be a direct image file URL — not a webpage URL
- Must be publicly accessible (return HTTP 200 with image content)

SEARCH STRATEGY — try each in order, stop as soon as you find a valid URL:

1. IMAGIN.studio CDN (best for white-background renders):
   Fetch this URL and check if it returns an image:
   https://cdn.imagin.studio/getimage?customer=de&make={mfr_slug}&modelFamily={model_slug}&angle=1
   Also try variations: angle=2, angle=7, customer=img, customer=uk

2. Official manufacturer press / media site:
   Search for "{mfr_en} {name_en} press photo official" on the manufacturer's media site
   (e.g. media.{mfr_slug}.com, press.{mfr_slug}.com, newsroom.{mfr_slug}.com)
   Fetch the page and extract the direct image URL from the HTML

3. Carwow press images:
   Search for "site:carwow.co.uk {mfr_en} {name_en}" and extract image URL from the car listing page

4. Autocar or TopGear official imagery:
   Search for "{mfr_en} {name_en} white background press photo site:autocar.co.uk OR site:topgear.com"

5. Google Images with white background filter:
   Search for: {mfr_en} {name_en} official white background car photo

IMPORTANT:
- After finding a candidate URL, fetch it (HEAD request) to confirm it works
- The image must be the EXACT model: {name_en} (not {mfr_en} 1500 if we want 2500, etc.)
- NEVER return: Wikipedia images, logos, badges, building photos, portraits
- If all strategies fail after genuine attempts, return exactly: NONE

Return ONLY the final image URL as plain text on a single line.
No explanation. No markdown. Just the URL (or NONE).
"""

    log.info(f"[image_agent] מחפש תמונה: {mfr_en} {name_en}...")
    raw = _run(client, prompt).strip()

    # Extract URL if model added surrounding text
    url_match = re.search(
        r'https?://\S+\.(?:jpg|jpeg|png|webp)(?:\?[^\s]*)?',
        raw, re.IGNORECASE,
    )
    url = url_match.group(0).rstrip(".,)>\"'") if url_match else ""

    if not url or raw.upper().startswith("NONE"):
        log.warning(f"[image_agent] לא נמצאה תמונה: {mfr_en} {name_en}")
        return ""

    if _verify(url):
        log.info(f"[image_agent] ✓ {name_en}: {url[:90]}")
        return url

    log.warning(f"[image_agent] URL לא עבר אימות ({url[:70]})")
    return ""


# ── standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    mfr  = sys.argv[1] if len(sys.argv) > 1 else "Toyota"
    name = sys.argv[2] if len(sys.argv) > 2 else "Corolla"
    he   = sys.argv[3] if len(sys.argv) > 3 else ""
    result = find(mfr, name, he)
    print(f"\nתוצאה: {result or '(לא נמצא)'}")
