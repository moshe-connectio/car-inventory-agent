"""
agents/model_agent.py

סוכן דגמים — מופעל על-ידי claude-opus-4-7 עם web_search.
שלב 1: בודק כל דגם מול מקורות ישראלים מגוונים.
שלב 2: מאמת עצמאית כל דגם שעומד לכיבוי לפני ביצוע.
"""

import json
import logging
import os
import re
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from zoho_client import update_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ModelAgent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

MODEL = "claude-opus-4-7"
MAX_TOKENS = 8192
MAX_CONTINUATIONS = 3


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY לא מוגדר בסביבה")
    return anthropic.Anthropic(api_key=api_key)


def _run_claude(client: anthropic.Anthropic, prompt: str) -> str:
    """מריץ קלוד עם web_search ומטפל ב-pause_turn אוטומטית."""
    messages = [{"role": "user", "content": prompt}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ]
    kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=tools,
        messages=messages,
    )

    response = client.messages.create(**kwargs)

    for _ in range(MAX_CONTINUATIONS):
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
        kwargs["messages"] = messages
        response = client.messages.create(**kwargs)

    return next((b.text for b in response.content if b.type == "text"), "")


def _parse_verdicts(text: str) -> dict[str, dict]:
    """מחלץ JSON מתשובת קלוד."""
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        log.warning(f"לא נמצא JSON: {text[:300]}")
        return {}
    try:
        data = json.loads(json_match.group())
        return data.get("verdicts", data)
    except json.JSONDecodeError as e:
        log.error(f"שגיאת JSON: {e}")
        return {}


def _verify_models(client: anthropic.Anthropic, mfr_en: str, models: list[dict]) -> dict[str, dict]:
    """סבב 1: בדיקת כל הדגמים מול מקורות ישראלים."""
    model_list = "\n".join(
        f"- {m.get('Name','')} (Hebrew: {m.get('Car_Model_Name_HE','')})"
        for m in models
    )

    prompt = f"""You are an expert on the Israeli car market. Verify which {mfr_en} models are currently sold in Israel in 2025-2026.

Models to check:
{model_list}

Search these sources:
- icar.co.il (Israel's main car guide)
- yad2.co.il new cars section
- Official {mfr_en} Israel importer website
- Israeli car news (e.g. zap.co.il, carsforum.co.il, walla.co.il/car)
- Any recent Israeli automotive articles (2024-2026)

A model is active=true if:
- Official Israeli importer lists it as available for purchase
- Has current 2024-2026 pricing or delivery options in Israel
- Was officially launched or sold in Israel recently

A model is active=false if:
- No official Israeli importer offers it
- Discontinued or never officially sold in Israel
- Only found in pre-2023 articles with no recent news

Return ONLY valid JSON:
{{
  "verdicts": {{
    "EXACT_ENGLISH_MODEL_NAME": {{
      "active": true,
      "confidence": "high",
      "source": "found on icar.co.il with current pricing"
    }}
  }}
}}"""

    text = _run_claude(client, prompt)
    log.info(f"[סבב 1] תשובת קלוד ({len(text)} תווים):\n{text[:600]}")
    return _parse_verdicts(text)


def _confirm_deactivations(
    client: anthropic.Anthropic, mfr_en: str, candidates: list[dict]
) -> set[str]:
    """סבב 2 (self-check): אימות דגמים שעומדים לכיבוי."""
    if not candidates:
        return set()

    model_list = "\n".join(
        f"- {m.get('Name','')} (Hebrew: {m.get('Car_Model_Name_HE','')})"
        for m in candidates
    )

    prompt = f"""CRITICAL VERIFICATION: I am about to mark the following {mfr_en} models as INACTIVE in our Israeli database.

Before proceeding, do a thorough search to confirm these are truly no longer sold in Israel.
Actively look for evidence they ARE still being sold (give benefit of the doubt).

Models pending deactivation:
{model_list}

Search specifically for:
- Any current {mfr_en} dealer or importer in Israel offering these models
- 2024-2026 news about these models in Israel
- Official {mfr_en} Israel website or authorized importers

Rules:
- If you find ANY current evidence of Israeli sales → active=true (keep active, do NOT deactivate)
- Only active=false if you are confident there is no current Israeli market presence
- When in doubt → keep active (active=true)

Return ONLY valid JSON:
{{
  "verdicts": {{
    "EXACT_ENGLISH_MODEL_NAME": {{
      "active": false,
      "confidence": "high",
      "source": "searched thoroughly, no evidence of current Israeli sales"
    }}
  }}
}}"""

    text = _run_claude(client, prompt)
    log.info(f"[סבב 2] תשובת קלוד ({len(text)} תווים):\n{text[:600]}")
    verdicts = _parse_verdicts(text)

    confirmed = set()
    for m in candidates:
        name = m.get("Name", "")
        v = verdicts.get(name, {})
        should_deactivate = (
            not v.get("active", True)
            and v.get("confidence", "low") in ("high", "medium")
        )
        if should_deactivate:
            confirmed.add(name)
            log.info(f"  ✓ אושר לכיבוי: {name} | {v.get('source','')}")
        else:
            log.info(f"  ⚠️ לא אושר — נשאר פעיל: {name} | {v.get('source','')}")

    return confirmed


def run(payload) -> dict:
    """
    payload: list[dict] — רשימת דגמים מזוהו.
    כל דגם מכיל: id, Name, Car_Model_Name_HE, Manufacturer, Active, ...
    """
    # --- פירוש payload ---
    if isinstance(payload, list):
        models = payload
    elif isinstance(payload, dict):
        models = payload.get("models", [payload])
    else:
        models = []

    if not models:
        return {"error": "אין דגמים לעיבוד"}

    mfr_name = models[0].get("Manufacturer", {}).get("name", "Unknown")

    log.info("══════════════════════════════════════")
    log.info(f"יצרן: {mfr_name} | דגמים: {len(models)}")
    log.info("══════════════════════════════════════")

    ai = _get_client()

    # --- סבב 1: בדיקה ראשונה ---
    verdicts = _verify_models(ai, mfr_name, models)

    # --- קטגוריזציה ---
    to_activate, to_deactivate_candidates = [], []
    for m in models:
        name = m.get("Name", "")
        is_active = bool(m.get("Active", False))
        v = verdicts.get(name, {})
        if not v:
            log.warning(f"אין verdict: {name}")
            continue
        should = v.get("active", is_active)
        if should and not is_active:
            to_activate.append(m)
        elif not should and is_active:
            to_deactivate_candidates.append(m)

    # --- סבב 2: אימות כיבויים ---
    confirmed_off = set()
    if to_deactivate_candidates:
        log.info(f"מאמת {len(to_deactivate_candidates)} כיבויים...")
        confirmed_off = _confirm_deactivations(ai, mfr_name, to_deactivate_candidates)

    # --- עדכון זוהו ---
    stats = {
        "manufacturer": mfr_name,
        "total": len(models),
        "activated": 0,
        "deactivated": 0,
        "no_change": 0,
        "errors": 0,
        "changes": [],
    }

    for m in models:
        name    = m.get("Name", "")
        mid     = m.get("id", "")
        name_he = m.get("Car_Model_Name_HE", "")
        active  = bool(m.get("Active", False))
        v       = verdicts.get(name, {})
        should  = v.get("active", active)

        if should and not active:
            try:
                update_model(mid, {"Active": True})
                stats["activated"] += 1
                stats["changes"].append({"name": name, "name_he": name_he, "action": "activated"})
                log.info(f"  ✅ הודלק: {name} ({name_he})")
            except Exception as e:
                stats["errors"] += 1
                log.error(f"  ❌ הדלקה נכשלה {name}: {e}")

        elif not should and active:
            if name in confirmed_off:
                try:
                    update_model(mid, {"Active": False})
                    stats["deactivated"] += 1
                    stats["changes"].append({"name": name, "name_he": name_he, "action": "deactivated"})
                    log.info(f"  🔴 כובה: {name} ({name_he})")
                except Exception as e:
                    stats["errors"] += 1
                    log.error(f"  ❌ כיבוי נכשל {name}: {e}")
            else:
                stats["no_change"] += 1

        else:
            stats["no_change"] += 1

    log.info("══════════════════════════════════════")
    log.info(
        f"סיכום {mfr_name}: {stats['total']} | "
        f"+{stats['activated']} הודלקו | "
        f"-{stats['deactivated']} כובו | "
        f"{stats['no_change']} ללא שינוי | "
        f"{stats['errors']} שגיאות"
    )
    log.info("══════════════════════════════════════")
    return stats
