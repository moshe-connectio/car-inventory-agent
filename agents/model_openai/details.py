"""
agents/model_openai/details.py
Pulse 2: per-model details (description, drivetrain, categories, year).
"""
import logging

from openai import OpenAI

from .utils import CAR_TYPES, VALID_CATEGORIES, ai_call, parse_json, normalize_car_type, clean_categories, strip_manufacturer_prefix

log = logging.getLogger(__name__)


def get_details(client: OpenAI, mfr_en: str, name_en: str, name_he: str, mfr_id: str) -> dict:
    """name_en may be empty when icar only provided Hebrew — AI fills it in."""
    valid_categories = ", ".join(sorted(VALID_CATEGORIES))

    name_en = strip_manufacturer_prefix(name_en, mfr_en)
    name_he = strip_manufacturer_prefix(name_he, mfr_en)

    if name_en:
        model_ref  = f"{mfr_en} {name_en} (Hebrew: {name_he})"
        name_field = f'"Name": "{name_en}"'
    else:
        model_ref  = f"{mfr_en} model known in Hebrew as: {name_he}"
        name_field = '"Name": "<model name only, without manufacturer — e.g. \\"2500\\" not \\"RAM 2500\\">"'

    prompt = f"""Fill in details for the {model_ref} as sold in Israel.

RESEARCH STEPS — use these sources in order:
1. Fetch https://www.icar.co.il/ and search for {mfr_en} {name_en or name_he} — get Israeli market data
   (year first sold in Israel, body type, drivetrain sold here)
2. Search: "{mfr_en} {name_en or name_he} ישראל יבואן" — find official Israeli importer page
3. SECONDARY: search auto.co.il for "{mfr_en} {name_en or name_he}" — for accurate technical specs
   (use only for description and drivetrain if icar lacks detail)

FILL IN:
1. Model name in English — WITHOUT the manufacturer brand (e.g. "2500" not "RAM 2500", "Seal 5" not "BYD Seal 5")
2. First year this exact model was officially sold in Israel (from icar or importer site)
3. Primary drivetrain sold in Israel — choose ONE from:
   חשמלי | היברידי | בנזין | דיזל | פלאג-אין היברידי
4. 2-3 sentence Hebrew description: body type, target buyer, standout features for Israeli buyer
5. Categories — choose ONLY from this exact list (can be multiple):
   {valid_categories}
   Examples: large electric SUV with 7 seats → ["SUV גדול", "חשמלי", "7 מושבים"]
             plug-in hybrid crossover        → ["קרוסאובר", "פלאג-אין היברידי"]
             compact petrol hatchback        → ["האצ'בק"]

JSON rules:
- No double-quote characters inside Hebrew string values — use single quotes if needed
- כ.ס instead of כ"ס
- Car_Type must be exactly one of: חשמלי, היברידי, בנזין, דיזל, פלאג-אין היברידי
- Category must be a JSON array using ONLY values from the list above
- Year_From must be the actual Israeli launch year (4-digit, e.g. "2022")

Return ONLY valid JSON:
{{
  {name_field},
  "Car_Model_Name_HE": "{name_he}",
  "Description": "תיאור בעברית...",
  "Car_Type": "בנזין",
  "Category": ["SUV", "חשמלי"],
  "Year_From": "2023",
  "Active": true,
  "Manufacturer": "{mfr_id}"
}}"""

    text = ai_call(client, prompt)
    label = name_en or name_he
    log.info(f"  [details] {label}: {len(text)} chars")
    data = parse_json(text)

    if name_en:
        data["Name"] = name_en
    elif not data.get("Name"):
        data["Name"] = name_he
    else:
        data["Name"] = strip_manufacturer_prefix(data["Name"], mfr_en)
    data["Car_Model_Name_HE"] = strip_manufacturer_prefix(
        data.get("Car_Model_Name_HE") or name_he, mfr_en
    )
    data.setdefault("Manufacturer", mfr_id)
    data["Car_Type"] = normalize_car_type(data.get("Car_Type", ""))
    data["Category"] = clean_categories(data.get("Category", []))
    return data
