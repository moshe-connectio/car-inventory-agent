"""
agents/trim_openai/fields.py
Single source of truth for trim fields, shared sources/price bounds, and sanity ranges.

FIELD_SPECS maps each Zoho field → AI JSON key → caster.
Used by both the Zoho record builder and the change-detector so the two never drift.
Field names match the Zoho API exactly (note: Battery_kwh / Charging_kw are lowercase).
"""

# ── Approved sources & price bounds (shared with fetcher + validator) ───────────
# Upper bound covers Israeli luxury/exotic new cars (Ferrari, Lamborghini, Rolls,
# high-end EVs) which routinely exceed ₪2-3M; still guards against USD/EUR or garbage.
MIN_PRICE = 55_000
MAX_PRICE = 8_000_000
APPROVED_SOURCES = ("icar.co.il", "auto.co.il", "gov.il")


# ── Casters ─────────────────────────────────────────────────────────────────────
def _int(v) -> int:
    return int(float(v))


def _float(v) -> float:
    return float(v)


def _str(v) -> str:
    return str(v).strip()


def _str_int(v) -> str:
    """Numeric value stored as a string in Zoho (HP, Range_km)."""
    return str(int(float(v)))


# ── Field map: (Zoho field, AI json key, caster) ────────────────────────────────
# Order matters only for log readability.
FIELD_SPECS: list[tuple[str, str, callable]] = [
    # ── core (pre-existing Zoho fields) ──
    ("Unit_Price",       "price",            _int),
    ("Monthly_Payment",  "monthly_payment",  _int),
    ("HP",               "hp",               _str_int),
    ("sec",              "sec",              _float),
    ("Range_km",         "range_km",         _str_int),
    ("Top_Speed",        "top_speed",        _int),
    ("Screen_inch",      "screen_inch",      _float),
    ("Doors",            "doors",            _int),
    ("Seats",            "seats",            _int),
    # ── 2026 enrichment (new Zoho fields) ──
    ("Engine_cc",        "engine_cc",        _int),
    ("Torque_Nm",        "torque_nm",        _int),
    ("Transmission",     "transmission",     _str),
    ("Drivetrain",       "drivetrain",       _str),
    ("Fuel_Type",        "fuel",             _str),
    ("Battery_kwh",      "battery_kwh",      _float),
    ("Charging_kw",      "charging_kw",      _int),
    ("Fuel_Consumption", "fuel_consumption", _float),
    ("Pollution_Level",  "pollution_level",  _int),
    ("Safety_Level",     "safety_level",     _int),
    ("Length_mm",        "length_mm",        _int),
    ("Width_mm",         "width_mm",         _int),
    ("Height_mm",        "height_mm",        _int),
    ("Trunk_Liters",     "trunk_liters",     _int),
    ("Weight_kg",        "weight_kg",        _int),
    ("Warranty",         "warranty",         _str),
]

# All AI spec keys (everything except the always-required price).
SPEC_KEYS: list[str] = [ai_key for _, ai_key, _ in FIELD_SPECS if ai_key != "price"]

# Zoho fields compared when deciding whether an existing trim changed.
CHANGE_FIELDS: list[str] = [zf for zf, _, _ in FIELD_SPECS] + ["Car_Finish_level_Name_EN"]


# ── Sanity ranges (inclusive) for numeric AI keys — used by the validator ───────
SANITY: dict[str, tuple[float, float]] = {
    "price":            (MIN_PRICE, MAX_PRICE),
    "monthly_payment":  (300, 60_000),
    "hp":               (30, 2000),
    "sec":              (1.5, 30),
    "range_km":         (10, 1200),   # low end covers PHEV electric-only range (~25 km)
    "top_speed":        (100, 420),
    "screen_inch":      (4, 30),
    "doors":            (2, 6),
    "seats":            (1, 9),
    "engine_cc":        (600, 9000),
    "torque_nm":        (50, 2000),
    "battery_kwh":      (5, 250),
    "charging_kw":      (3, 400),
    "fuel_consumption": (3, 50),
    "pollution_level":  (1, 15),
    "safety_level":     (0, 8),
    "length_mm":        (2500, 6500),
    "width_mm":         (1400, 2300),
    "height_mm":        (1100, 2200),
    "trunk_liters":     (50, 4000),
    "weight_kg":        (600, 4500),
}


# ── Drivetrain normalisation → Hebrew canonical values ──────────────────────────
DRIVETRAIN_VALUES = {"קדמית", "אחורית", "4X4"}
_DRIVETRAIN_ALIASES = {
    "fwd": "קדמית", "front": "קדמית", "front-wheel drive": "קדמית", "קידמית": "קדמית",
    "rwd": "אחורית", "rear": "אחורית", "rear-wheel drive": "אחורית",
    "awd": "4X4", "4wd": "4X4", "4x4": "4X4", "4×4": "4X4",
    "all-wheel drive": "4X4", "כפולה": "4X4", "4 על 4": "4X4",
}


def normalize_drivetrain(v: str) -> str | None:
    """Map a free-text drivetrain to a canonical Hebrew value, or None if unknown."""
    s = (v or "").strip()
    if s in DRIVETRAIN_VALUES:
        return s
    return _DRIVETRAIN_ALIASES.get(s.lower())
