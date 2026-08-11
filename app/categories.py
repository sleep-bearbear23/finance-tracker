"""Category taxonomy + a cheap keyword guess used at ingest time."""
from __future__ import annotations

import re

CATEGORIES = [
    "Food & Groceries",
    "Eating Out",
    "Coffee",
    "Rideshare & Transit",
    "Gas",
    "Rent & Utilities",
    "Subscriptions",
    "Shopping",
    "Beauty",
    "Pets",
    "Art & Work Supplies",
    "Health",
    "Fun & Going Out",
    "Travel",
    "Gifts",
    "Life Admin",
    "Income",
    "Transfers/Ignore",
]

# merchant keyword -> category. First match wins. Lowercased contains-match.
_RULES: list[tuple[str, str]] = [
    ("whole foods", "Food & Groceries"),
    ("trader joe", "Food & Groceries"),
    ("ralphs", "Food & Groceries"),
    ("safeway", "Food & Groceries"),
    ("99 ranch", "Food & Groceries"),
    ("h mart", "Food & Groceries"),
    ("costco", "Food & Groceries"),
    ("mcdonald", "Eating Out"),
    ("chipotle", "Eating Out"),
    ("uber eats", "Eating Out"),
    ("uber *eats", "Eating Out"),
    ("ubereats", "Eating Out"),
    ("doordash", "Eating Out"),
    ("grubhub", "Eating Out"),
    ("restaurant", "Eating Out"),
    ("starbucks", "Coffee"),
    ("blue bottle", "Coffee"),
    ("coffee", "Coffee"),
    ("philz", "Coffee"),
    ("uber", "Rideshare & Transit"),
    ("lyft", "Rideshare & Transit"),
    ("metro", "Rideshare & Transit"),
    ("chevron", "Gas"),
    ("shell", "Gas"),
    ("76 ", "Gas"),
    ("arco", "Gas"),
    ("gas", "Gas"),
    ("netflix", "Subscriptions"),
    ("spotify", "Subscriptions"),
    ("adobe", "Subscriptions"),
    ("openai", "Subscriptions"),
    ("anthropic", "Subscriptions"),
    ("icloud", "Subscriptions"),
    ("google storage", "Subscriptions"),
    ("edison", "Rent & Utilities"),
    ("socalgas", "Rent & Utilities"),
    ("at&t", "Rent & Utilities"),
    ("t-mobile", "Rent & Utilities"),
    ("verizon", "Rent & Utilities"),
    ("rent", "Rent & Utilities"),
    ("sephora", "Shopping"),
    ("amazon", "Shopping"),
    ("target", "Shopping"),
    ("uniqlo", "Shopping"),
    ("blick", "Art & Work Supplies"),
    ("michaels", "Art & Work Supplies"),
    ("home depot", "Art & Work Supplies"),
    ("cvs", "Health"),
    ("walgreens", "Health"),
    ("pharmacy", "Health"),
]


# Regex rules learned from 19 months of Momo's real statements — much wider net than _RULES.
# Order matters: brands first (most specific), then cuisine/boba/parking heuristics.
_RX_RULES: list[tuple[str, str]] = [
    (r"hungrypanda|uber ?\*?eats|grubhub|doordash|postmates", "Eating Out"),
    (r"\blyft\b|\buber\b|youbike|\bmetro\b", "Rideshare & Transit"),
    (r"parking|tollroad|pnm\*tca|frogparking|\blaz \b|citation", "Rideshare & Transit"),
    (r"99 ranch|nijiya|weee|\byami\b|instacart|trader joe|costco|ralphs|vons|albertsons|safeway|"
     r"sprouts|gelsons|marukai|mitsuwa|tokyo central|toyko central|walmart|h ?mart|whole ?f(oo)?ds|sheng kee", "Food & Groceries"),
    (r"mcdonald|popeyes|jollibee|in-?n-?out|raising canes|chipotle|kfc\b|taco bell|wingstop|"
     r"shake shack|chick-?fil|panda express|jack in the box", "Eating Out"),
    (r"tst[\* ]|dumpling|kitchen|restaur|ramen|sushi|izakaya|bbq|grill|pho\b|noodle|hot ?pot|"
     r"szechuan|sichuan|dim ?sum|bistro|thai|pizza|taco|burger|chicken|tofu|gopchang|jangguk|"
     r"poke|deli\b|eatery|diner|seafood|shabu|curry|udon|soba|yakitori|katsu|wings|bao\b|congee|"
     r"porridge|malatang|skewer|banh|kbbq|soondubu|kopitiam|uep\*|trattoria|osteria|ristor|"
     r"dining|dinning|bakery|baguette|85c|jj bakery|portos|beard papas", "Eating Out"),
    (r"starbucks|coffee|espresso|latte|boba|sharetea|gong ?cha|yifang|milk ?tea|heytea|tp ?tea|"
     r"machi|tiger sugar|kung fu tea|sunright|meet fresh|chagee|molly tea|chicha|wushiland|"
     r"pot of cha|dessert|ice ?cream|creamery|\btea\b|philz|blue bottle", "Coffee"),
    (r"netflix|spotify|crunchyroll|lezhin|patreon|youtube", "Subscriptions"),
    (r"adobe|midjourney|\bxai\b|grok|openai|chatgpt|anthropic|claude|comfy\.org|notion labs|"
     r"usemotion|visualcrossing|pephop|icloud|apple\.com/bill|google one|google storage", "Subscriptions"),
    (r"ultra mobile|geico|edison|socalgas|at&t|t-mobile|verizon|public storage", "Rent & Utilities"),
    (r"arco|shell oil|chevron|exxon|sinclair|mobil|speedway|gasoline|\bfuel\b|\b76\b|rocket \d", "Gas"),
    (r"michaels|blick|home ?depot|lowes|staples|1 hour photo|printing|sharegrid|ups store|"
     r"flower|bloom connection|st\.? vincent", "Art & Work Supplies"),
    (r"\bcvs\b|walgreens|rite aid|pharmacy|uscis", "Health"),
    (r"petco|petsmart|chewy|pet food", "Pets"),
    (r"sephora|ulta\b|olive young|sally beauty|hair salon|\bnail|barber|superbcut", "Beauty"),
    (r"uniqlo|zara\b|h&m|hm\.com|free people|ross stores|goodwill|windsor|hollister|"
     r"american eagle|lovisa|casetify|vintage|thrift|daiso|ikea|bed bath|apple store|"
     r"target|amazon|amzn|ebay|etsy", "Shopping"),
    (r"steam|playstation|nintendo|\bamc\b|regal|cinemark|cinema|laemmle|museum|ticketmaster|"
     r"\btm \*|ticket tailor|eventbrite|axs\b|fanatics|brewery|brewing|tavern|\bbar\b|karaoke|ktv", "Fun & Going Out"),
    (r"airbnb|united air|starlux|eva air|china air|delta air|\bhotel\b|hostel|\btwn\b|taipei|"
     r"hsinchu|taoyuan|kaohsiung|taichung|\bfra\b|\bcdg\b|airport|airline", "Travel"),
    (r"\bdmv\b|court|city of |county of ", "Life Admin"),
]

_RX_COMPILED = [(re.compile(p), c) for p, c in _RX_RULES]


def guess(merchant: str) -> str | None:
    m = (merchant or "").lower()
    for kw, cat in _RULES:
        if kw in m:
            return cat
    for rx, cat in _RX_COMPILED:
        if rx.search(m):
            return cat
    return None


# Descriptions that mean "money just moved between accounts" — a credit-card payment,
# an internal transfer, autopay. These are NOT spending and NOT income; they're noise.
_TRANSFER_KEYWORDS = [
    "payment thank you", "payment - thank you", "autopay", "auto pay", "auto-pay",
    "online payment", "mobile payment", "web payment", "web pymt", "bill pay",
    "card payment", "credit card payment", "crd payment", "pymt", "epay", "e-payment",
    "ach payment", "ach pmt", "chase credit crd", "internal transfer", "online transfer",
    "acct transfer", "acct xfer", "account transfer", "transfer to", "transfer from",
    "to savings", "from savings", "to checking", "from checking",
    # paying the Apple Card from Chase shows up like this — money moving, not spending
    "applecard", "apple card ach", "gsbank", "goldman sachs bank", "ach deposit internet transfer",
    "venmo cashout", "venmo payment",
]


FIXED_HINT = {"Rent & Utilities", "Subscriptions"}  # categories that are recurring/necessary


def is_transfer(merchant: str) -> bool:
    m = (merchant or "").lower()
    return any(kw in m for kw in _TRANSFER_KEYWORDS)


def merchant_key(merchant: str) -> str:
    """Normalize a raw description into a stable key for merchant memory + dedup.
    Drops digits, spaces and punctuation so 'WHOLEFOODS #382' and 'Whole Foods' match,
    but keeps sender names distinct so 'ZELLE FROM JOHN' ≠ 'ZELLE FROM MARY'."""
    s = (merchant or "").lower()
    s = re.sub(r"[^a-z一-鿿]+", "", s)  # keep only letters (incl. Chinese)
    return s[:120] or "unknown"
