"""
agents/trim_openai/validator.py
Deterministic validation gate — runs after the fetch and before any Zoho write.

No AI call. Checks that the required data arrived and that every value is sane:
  • fatal checks (reject the whole trim): name, price in range, approved source
  • spec checks (drop the bad field, keep the trim): each numeric value must be
    inside its SANITY range; drivetrain must normalise to a known value.

Only trims that pass the fatal checks are written. A model is "complete" (eligible
for deactivating stale trims) only if at least one trim passed.
"""
import logging

from .fields import (
    APPROVED_SOURCES,
    SANITY,
    SPEC_KEYS,
    normalize_drivetrain,
)

log = logging.getLogger(__name__)


def validate_trim(trim: dict) -> tuple[bool, list[str]]:
    """
    Validate (and clean in-place) one trim.
    Returns (ok, issues). ok=False means the trim is rejected entirely.
    Out-of-range spec values are set to None (field dropped) and noted in issues.
    """
    issues: list[str] = []

    # ── fatal: name ───────────────────────────────────────────────
    name_he = (trim.get("name_he") or "").strip()
    if not name_he:
        return False, ["name_he ריק"]

    # ── fatal: approved source ────────────────────────────────────
    src = (trim.get("source_url") or "").lower()
    if not any(s in src for s in APPROVED_SOURCES):
        return False, [f"מקור לא מאושר: {src!r}"]

    # ── fatal: price ──────────────────────────────────────────────
    # icar/auto ARE pricing sources → an in-range price is mandatory. gov.il has no
    # prices (fallback for manufacturers absent from icar) → a missing price is allowed
    # and the field stays empty; but if a price IS present it must still be sane.
    price    = trim.get("price")
    gov_only = "gov.il" in src and "icar.co.il" not in src and "auto.co.il" not in src
    lo, hi   = SANITY["price"]
    if price is None or price == "":
        if not gov_only:
            return False, ["מחיר חסר ממקור תמחור"]
    elif not isinstance(price, (int, float)) or not (lo <= price <= hi):
        return False, [f"מחיר לא תקין: {price!r}"]

    # ── non-fatal: drivetrain normalisation ───────────────────────
    if trim.get("drivetrain"):
        norm = normalize_drivetrain(trim["drivetrain"])
        if norm:
            trim["drivetrain"] = norm
        else:
            issues.append(f"drivetrain לא מזוהה: {trim['drivetrain']!r} — נוקה")
            trim["drivetrain"] = None

    # ── non-fatal: numeric sanity for every spec ──────────────────
    for key in SPEC_KEYS:
        if key not in SANITY:
            continue
        v = trim.get(key)
        if v is None:
            continue
        if not isinstance(v, (int, float)):
            issues.append(f"{key} לא מספרי: {v!r} — נוקה")
            trim[key] = None
            continue
        lo, hi = SANITY[key]
        if not (lo <= v <= hi):
            issues.append(f"{key}={v} מחוץ לטווח [{lo},{hi}] — נוקה")
            trim[key] = None

    return True, issues


def validate_fetch(trims: list[dict]) -> dict:
    """
    Split fetched trims into valid / rejected and report completeness.
    complete=True (≥1 valid trim) is required before deactivating stale Zoho trims.
    """
    valid: list[dict] = []
    rejected: list[tuple[dict, list[str]]] = []

    for t in trims:
        ok, issues = validate_trim(t)
        if ok:
            valid.append(t)
            if issues:
                log.info(f"     [validate] {t.get('name_he','')}: ניקוי {issues}")
        else:
            rejected.append((t, issues))
            log.warning(f"     [validate] נדחה '{t.get('name_he','?')}': {issues}")

    return {
        "valid":    valid,
        "rejected": rejected,
        "complete": len(valid) >= 1,
    }
