"""Merchant -> category: a learning map, stored in the DB.

Order for each transaction (first hit wins):
  1. the person's own fix          merchant_map(owner=<pid>, key)          source "user"
  2. starter rules (this file)      regexes for common US chains            source "seed"
  3. the shared learned map         merchant_map(owner='', key)             source "bank" | "ai"
  4. the bank's own category        e.g. Discover's "Restaurants", which is also learned into (3)
  5. Gemini (free tier)             only the merchant names, batched; the answers are learned into (3)
Nothing found (e.g. Gemini down) leaves it uncategorized; the daily `spendcat` job retries.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

from app.db import DB

log = logging.getLogger(__name__)

CATEGORIES = ["Groceries", "Dining", "Gas", "Transport", "Travel", "Shopping", "Home", "Utilities & phone",
              "Subscriptions", "Entertainment", "Health", "Personal care", "Kids & education", "Gifts & donations",
              "Insurance", "Fees & interest", "Other"]

# (pattern on the upper-cased description, canonical merchant name, category). Public chains only.
SEED: list[tuple[str, str, str]] = [
    # specific before general (first match wins)
    (r"^TST ?\*", "", "Dining"),                     # Toast: a restaurant point-of-sale
    (r"AMAZON PRIME|PRIME VIDEO|AUDIBLE|KINDLE", "AMAZON PRIME", "Subscriptions"),
    (r"COSTCO GAS", "COSTCO GAS", "Gas"), (r"COSTCO\.COM", "COSTCO.COM", "Shopping"),
    # groceries
    (r"SAFEWAY", "SAFEWAY", "Groceries"), (r"TRADER JOE", "TRADER JOE'S", "Groceries"),
    (r"WHOLE ?FOODS|WFM", "WHOLE FOODS", "Groceries"), (r"COSTCO", "COSTCO", "Groceries"),
    (r"SPROUTS", "SPROUTS", "Groceries"), (r"\bLUCKY( SUPERMARKET| STORES?\b| ?#| \d)", "LUCKY", "Groceries"),
    (r"\bRALEY", "RALEY'S", "Groceries"),
    (r"KROGER", "KROGER", "Groceries"), (r"\bALDI\b", "ALDI", "Groceries"), (r"H ?MART", "H MART", "Groceries"),
    (r"99 RANCH", "99 RANCH", "Groceries"), (r"\bMITSUWA", "MITSUWA", "Groceries"), (r"NIJIYA", "NIJIYA", "Groceries"),
    (r"PUBLIX", "PUBLIX", "Groceries"), (r"\bVONS\b", "VONS", "Groceries"), (r"\bALBERTSONS", "ALBERTSONS", "Groceries"),
    (r"SAM'?S CLUB", "SAM'S CLUB", "Groceries"), (r"INSTACART", "INSTACART", "Groceries"),
    (r"GROCERY OUTLET", "GROCERY OUTLET", "Groceries"),
    (r"DRAEGER", "DRAEGER'S", "Groceries"), (r"NOB HILL", "NOB HILL", "Groceries"), (r"\bWEEE\b", "WEEE!", "Groceries"),
    # dining
    (r"STARBUCKS", "STARBUCKS", "Dining"), (r"PEET'?S", "PEET'S", "Dining"), (r"BLUE BOTTLE", "BLUE BOTTLE", "Dining"),
    (r"PHILZ", "PHILZ", "Dining"), (r"DUNKIN", "DUNKIN'", "Dining"), (r"MCDONALD", "MCDONALD'S", "Dining"),
    (r"CHIPOTLE", "CHIPOTLE", "Dining"), (r"IN.N.OUT", "IN-N-OUT", "Dining"), (r"PANERA", "PANERA", "Dining"),
    (r"SUBWAY", "SUBWAY", "Dining"), (r"TACO BELL", "TACO BELL", "Dining"), (r"CHICK.FIL.A", "CHICK-FIL-A", "Dining"),
    (r"PANDA EXPRESS", "PANDA EXPRESS", "Dining"), (r"SWEETGREEN", "SWEETGREEN", "Dining"), (r"DOMINO", "DOMINO'S", "Dining"),
    (r"PIZZA HUT", "PIZZA HUT", "Dining"), (r"SHAKE SHACK", "SHAKE SHACK", "Dining"), (r"FIVE GUYS", "FIVE GUYS", "Dining"),
    (r"DOORDASH", "DOORDASH", "Dining"), (r"UBER ?\*? ?EATS", "UBER EATS", "Dining"), (r"GRUBHUB", "GRUBHUB", "Dining"),
    (r"\bCAVA\b", "CAVA", "Dining"), (r"TAPIOCA|BOBA|SHARETEA|GONG CHA|TP TEA|KUNG FU TEA", "BUBBLE TEA", "Dining"),
    (r"RESTAURANT|CAFE\b|BAKERY|DIM SUM|SUSHI|RAMEN|PHO\b|BBQ|GRILL|KITCHEN|BISTRO|TAQUERIA|PIZZ", "", "Dining"),
    # gas / ev
    (r"\bSHELL\b", "SHELL", "Gas"), (r"CHEVRON", "CHEVRON", "Gas"), (r"\bARCO\b", "ARCO", "Gas"),
    (r"^76\b|\b76 (GAS|STATION|FUEL)|UNION 76", "76", "Gas"),
    (r"VALERO", "VALERO", "Gas"), (r"EXXON|MOBIL\b", "EXXONMOBIL", "Gas"),
    (r"\bBP\b|AMOCO", "BP", "Gas"), (r"SPEEDWAY", "SPEEDWAY", "Gas"), (r"TESLA SUPERCHARG", "TESLA SUPERCHARGER", "Gas"),
    (r"CHARGEPOINT|ELECTRIFY AMERICA|EVGO", "EV CHARGING", "Gas"),
    # transport
    (r"\bUBER\b(?!.*EATS)", "UBER", "Transport"), (r"\bLYFT\b", "LYFT", "Transport"), (r"CALTRAIN", "CALTRAIN", "Transport"),
    (r"\bBART\b|CLIPPER", "CLIPPER/BART", "Transport"), (r"FASTRAK|BAY AREA TOLL", "FASTRAK", "Transport"),
    (r"PARKING|PARKMOBILE|SPOTHERO", "PARKING", "Transport"), (r"JIFFY LUBE|CAR WASH|AUTOZONE|PEP BOYS", "", "Transport"),
    (r"DMV", "DMV", "Transport"),
    # travel
    (r"UNITED\s?AIR|UNITED 0", "UNITED AIRLINES", "Travel"), (r"DELTA AIR", "DELTA", "Travel"),
    (r"ALASKA AIR", "ALASKA AIRLINES", "Travel"),
    (r"SOUTHWEST", "SOUTHWEST", "Travel"), (r"AMERICAN AIR", "AMERICAN AIRLINES", "Travel"), (r"JETBLUE", "JETBLUE", "Travel"),
    (r"AIRBNB", "AIRBNB", "Travel"), (r"VRBO", "VRBO", "Travel"), (r"EXPEDIA", "EXPEDIA", "Travel"),
    (r"BOOKING\.COM", "BOOKING.COM", "Travel"),
    (r"MARRIOTT", "MARRIOTT", "Travel"), (r"HILTON", "HILTON", "Travel"), (r"HYATT", "HYATT", "Travel"),
    (r"\bIHG\b|HOLIDAY INN", "IHG", "Travel"),
    (r"HERTZ", "HERTZ", "Travel"), (r"ENTERPRISE RENT|AVIS\b|BUDGET RENT|NATIONAL CAR", "CAR RENTAL", "Travel"),
    (r"HOTEL|RESORT|AIRLINE|AIRPORT", "", "Travel"),
    # shopping
    (r"AMAZON|AMZN MKTP|AMZN DIGITAL|AMZN.COM", "AMAZON", "Shopping"),
    (r"TARGET", "TARGET", "Shopping"), (r"WAL-?MART", "WALMART", "Shopping"),
    (r"\bEBAY\b", "EBAY", "Shopping"), (r"ETSY", "ETSY", "Shopping"), (r"BEST BUY", "BEST BUY", "Shopping"),
    (r"APPLE STORE|APPLE\.COM(?!/BILL)", "APPLE STORE", "Shopping"), (r"\bIKEA\b", "IKEA", "Home"), (r"MACY", "MACY'S", "Shopping"),
    (r"NORDSTROM", "NORDSTROM", "Shopping"), (r"\bTJ ?MAXX|MARSHALLS|HOMEGOODS|ROSS STORE", "TJX/ROSS", "Shopping"),
    (r"\bUNIQLO", "UNIQLO", "Shopping"), (r"\bH&M\b", "H&M", "Shopping"), (r"\bZARA\b", "ZARA", "Shopping"),
    (r"\bGAP\b|OLD NAVY", "GAP", "Shopping"),
    (r"NIKE", "NIKE", "Shopping"), (r"\bREI\b", "REI", "Shopping"), (r"TEMU", "TEMU", "Shopping"), (r"SHEIN", "SHEIN", "Shopping"),
    (r"DAISO", "DAISO", "Shopping"), (r"MICHAELS", "MICHAELS", "Shopping"),
    # home
    (r"HOME DEPOT", "HOME DEPOT", "Home"), (r"LOWE'?S", "LOWE'S", "Home"), (r"ORCHARD SUPPLY|ACE HARDWARE", "HARDWARE", "Home"),
    (r"WAYFAIR", "WAYFAIR", "Home"), (r"BED BATH|CRATE.{0,3}BARREL|WILLIAMS.SONOMA|POTTERY BARN|WEST ELM", "HOME GOODS", "Home"),
    # utilities & phone
    (r"PG&E|PGANDE|PACIFIC GAS", "PG&E", "Utilities & phone"), (r"COMCAST|XFINITY", "XFINITY", "Utilities & phone"),
    (r"AT&T|\bATT\b", "AT&T", "Utilities & phone"), (r"VERIZON", "VERIZON", "Utilities & phone"),
    (r"T-MOBILE|TMOBILE", "T-MOBILE", "Utilities & phone"),
    (r"MINT MOBILE|GOOGLE ?\*?FI\b|VISIBLE", "MOBILE PLAN", "Utilities & phone"),
    (r"SONIC\.NET|SONIC TELECOM", "SONIC", "Utilities & phone"),
    (r"WATER (DIST|DEPT|SERVICE)|RECOLOGY|WASTE MANAGEMENT", "WATER/WASTE", "Utilities & phone"),
    # subscriptions
    (r"NETFLIX", "NETFLIX", "Subscriptions"), (r"SPOTIFY", "SPOTIFY", "Subscriptions"), (r"HULU", "HULU", "Subscriptions"),
    (r"DISNEY ?PLUS|DISNEY\+", "DISNEY+", "Subscriptions"), (r"HBO ?MAX|\bMAX\.COM", "MAX", "Subscriptions"),
    (r"YOUTUBE", "YOUTUBE", "Subscriptions"), (r"APPLE\.COM/BILL|ITUNES", "APPLE SERVICES", "Subscriptions"),
    (r"GOOGLE \*?(STORAGE|ONE|GSUITE|WORKSPACE)|GOOGLE \*GOOGLE", "GOOGLE SERVICES", "Subscriptions"),
    (r"MICROSOFT|XBOX", "MICROSOFT", "Subscriptions"),
    (r"ADOBE", "ADOBE", "Subscriptions"), (r"DROPBOX", "DROPBOX", "Subscriptions"), (r"OPENAI|CHATGPT", "OPENAI", "Subscriptions"),
    (r"ANTHROPIC|CLAUDE\.AI", "ANTHROPIC", "Subscriptions"), (r"NYTIMES|NEW YORK TIMES|WSJ|WALL STREET JOURNAL", "NEWS", "Subscriptions"),
    (r"ORACLE.*CLOUD|\bOCI\b", "ORACLE CLOUD", "Subscriptions"), (r"PELOTON", "PELOTON", "Subscriptions"),
    # entertainment
    (r"AMC THEAT|CINEMARK|REGAL CIN|FANDANGO", "MOVIES", "Entertainment"), (r"TICKETMASTER|STUBHUB|EVENTBRITE", "TICKETS", "Entertainment"),
    (r"STEAM ?GAMES|STEAMPOWERED|NINTENDO|PLAYSTATION", "GAMES", "Entertainment"),
    (r"DISNEYLAND|UNIVERSAL STUDIOS|SIX FLAGS", "THEME PARK", "Entertainment"),
    (r"MUSEUM|AQUARIUM|\bZOO\b", "", "Entertainment"),
    # health / personal care
    (r"\bCVS\b", "CVS", "Health"), (r"WALGREENS", "WALGREENS", "Health"), (r"RITE AID", "RITE AID", "Health"),
    (r"KAISER|SUTTER|STANFORD HEALTH|PALO ALTO MEDICAL|ONE MEDICAL", "MEDICAL", "Health"),
    (r"DENTAL|DENTIST|ORTHODON|PHARMACY|OPTOMETR|VISION|MEDICAL|CLINIC|HOSPITAL", "", "Health"),
    (r"24 HOUR FITNESS|EQUINOX|PLANET FITNESS|\bYMCA\b|CLASSPASS", "GYM", "Health"),
    (r"SEPHORA", "SEPHORA", "Personal care"), (r"ULTA", "ULTA", "Personal care"),
    (r"GREAT CLIPS|SUPERCUTS|SALON|BARBER|NAIL|SPA\b", "", "Personal care"),
    # kids & education / gifts / insurance
    (r"KUMON|TUTOR|ACADEMY|SCHOOL|UNIVERSITY|COLLEGE|UDEMY|COURSERA", "", "Kids & education"), (r"TOYS?\b|LEGO", "", "Kids & education"),
    (r"GOFUNDME|DONATION|RED CROSS|UNICEF|CHARITY|\bPBS\b|KQED", "", "Gifts & donations"),
    (r"GEICO|STATE FARM|PROGRESSIVE|ALLSTATE|LEMONADE|INSURANCE", "", "Insurance"),
]
_SEED = [(re.compile(p), name, cat) for p, name, cat in SEED]

BANK_CATEGORIES = {   # issuer categories (Discover today) -> ours; unclear ones are left to the other sources
    "restaurants": "Dining", "supermarkets": "Groceries", "grocery stores": "Groceries", "warehouse clubs": "Groceries",
    "gasoline": "Gas", "gas stations": "Gas", "department stores": "Shopping", "merchandise": "Shopping",
    "home improvement": "Home", "medical services": "Health", "education": "Kids & education", "automotive": "Transport",
    "travel/ entertainment": "Travel", "travel/entertainment": "Travel", "airfare": "Travel", "hotels": "Travel",
    "car rental": "Travel", "utilities": "Utilities & phone", "entertainment": "Entertainment", "streaming": "Subscriptions",
}
PREFIX = re.compile(r"^(SQ ?\*|TST ?\*|SP ?\*?|PY ?\*|PP ?\*|PAYPAL ?\*|IC ?\*|BT ?\*|CKO ?\*|FS ?\*|SMK ?\*|CLR ?\*)\s*")


def seed_match(desc: str) -> tuple[str, str] | None:
    d = desc.upper()
    for rx, name, cat in _SEED:
        if rx.search(d):
            return name, cat
    return None


def merchant_key(desc: str) -> str:
    """Stable grouping key: the seed's canonical name, else the first words without processor prefixes,
    store numbers, '.COM' and a trailing state code. E.g. 'SQ *BLUE DOOR CAFE #12 SPRINGFIELD CA' -> 'BLUE DOOR'."""
    if (hit := seed_match(desc)) and hit[0]:
        return hit[0]
    d = PREFIX.sub("", desc.upper().strip())
    d = re.sub(r"\.COM\b", "", d.replace("*", " "))
    words = [w for w in re.split(r"\s+", d) if w and not re.search(r"\d", w) and w not in {"#", "-", "&"}]
    if len(words) > 1 and re.fullmatch(r"[A-Z]{2}", words[-1]):
        words = words[:-1]                                  # state code
    keep = words[:3] if len(words) > 2 and len(words[1]) <= 2 else words[:2]   # "TRADER JOE S"
    return " ".join(keep) or desc.upper()[:24]


def bank_category(raw: str) -> str | None:
    return BANK_CATEGORIES.get(re.sub(r"\s+", " ", raw.strip().lower()))


# ---- the stored map -----------------------------------------------------------------------------

def lookup(db: DB, owner: str, key: str) -> tuple[str, str] | None:
    """(category, source): the person's own fix first, then the shared learned map."""
    rows = db.rows("SELECT owner, category, source FROM merchant_map WHERE key=? AND owner IN (?, '') "
                   "ORDER BY owner = '' LIMIT 1", (key, owner))
    return (rows[0]["category"], rows[0]["source"]) if rows else None


def learn(db: DB, owner: str, key: str, category: str, source: str) -> None:
    """Upsert. A person's fixes are their own rows (owner=pid), so shared learning never touches them;
    in the shared map an 'ai' guess never replaces a 'bank' category."""
    with db.tx() as c:
        if source == "ai" and c.execute("SELECT 1 FROM merchant_map WHERE owner=? AND key=? AND source='bank'", (owner, key)).fetchone():
            return
        c.execute("INSERT INTO merchant_map(owner, key, category, source, updated) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(owner, key) DO UPDATE SET category=excluded.category, source=excluded.source, "
                  "updated=excluded.updated", (owner, key, category, source, datetime.now(UTC).isoformat()))


def resolve(db: DB, owner: str, desc: str, raw_bank_category: str = "") -> tuple[str, str] | None:
    """(category, source) without AI, learning a bank category on the way; None = needs AI."""
    key = merchant_key(desc)
    if (hit := lookup(db, owner, key)) and hit[1] == "user":
        return hit
    if (seed := seed_match(desc)):
        return seed[1], "seed"
    if hit:
        return hit
    if raw_bank_category and (cat := bank_category(raw_bank_category)):
        learn(db, "", key, cat, "bank")
        return cat, "bank"
    return None


PROMPT = """Categorize each US credit card merchant (as printed on a statement) into exactly one of these
categories: {cats}.
Use "Other" when unsure. Return ONLY JSON: {{"merchants": {{"<merchant>": "<category>"}}}}

Merchants:
{names}"""


def ask_ai(names: list[str], llm: Callable[[str], str | None] | None, batch: int = 40) -> dict[str, str]:
    """Merchant names only (no amounts, dates or people). Unknown/invalid answers are dropped."""
    out: dict[str, str] = {}
    if not llm:
        return out
    for i in range(0, len(names), batch):
        part = names[i:i + batch]
        raw = llm(PROMPT.format(cats=", ".join(CATEGORIES), names="\n".join(f"- {n}" for n in part)))
        try:
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip() or "{}")
        except json.JSONDecodeError:
            log.warning("Merchant categories: non-JSON reply")
            continue
        for name, cat in (data.get("merchants") or {}).items() if isinstance(data, dict) else []:
            if name in part and cat in CATEGORIES:
                out[name] = cat
    return out


# ---- display names ("SQ *BLUE DOOR CAFE #12" is grouped as BLUE DOOR and shown as "Blue Door") --------

NICE = {"DOORDASH": "DoorDash", "YOUTUBE": "YouTube", "EBAY": "eBay", "IKEA": "IKEA", "OPENAI": "OpenAI",
        "CHICK-FIL-A": "Chick-fil-A", "IN-N-OUT": "In-N-Out", "WEEE!": "Weee!", "MCDONALD'S": "McDonald's",
        "TJX/ROSS": "TJ Maxx / Ross", "FASTRAK": "FasTrak", "CLIPPER/BART": "Clipper/BART", "EV CHARGING": "EV charging",
        "T-MOBILE": "T-Mobile", "XFINITY": "Xfinity", "UBER EATS": "Uber Eats", "AMAZON PRIME": "Amazon Prime",
        "BOOKING.COM": "Booking.com", "COSTCO.COM": "Costco.com", "HBO": "HBO", "DISNEY+": "Disney+"}


def pretty(key: str) -> str:
    if key in NICE:
        return NICE[key]
    out = []
    for w in key.split():
        if "&" in w or re.fullmatch(r"[A-Z0-9]{1,3}", w) and not re.search(r"[AEIOU]", w[1:] if len(w) > 1 else "A"):
            out.append(w)                                   # AT&T, PG&E, CVS, BP, 76
        else:
            out.append(re.sub(r"[A-Z]+", lambda m: m.group().capitalize() if m.start() == 0 else m.group().lower(), w))
    return " ".join(out)


def display_name(db: DB, owner: str, key: str) -> str:
    rows = db.rows("SELECT display FROM merchant_names WHERE owner=? AND key=?", (owner, key))
    return rows[0]["display"] if rows else pretty(key)


def rename(db: DB, owner: str, key: str, display: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO merchant_names(owner, key, display) VALUES (?,?,?) "
                  "ON CONFLICT(owner, key) DO UPDATE SET display=excluded.display", (owner, key, display.strip()[:40]))
