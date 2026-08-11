"""The category taxonomy — and the *treatment tags* the budget math actually reads.

The old list was a flat pile of names, so every calculation had to hard-code which
strings meant "rent-like" and which meant "coffee-like". That broke every time a
category was added. Here each category carries a treatment tag, and the allowance
math only ever asks for the tag:

  fixed      happens whether or not Momo decides anything — subtracted before allowance
  flex       the daily decisions she comments on          — counted against allowance
  want       hobby / pure want                            — counted against allowance
  work       cost of doing business                       — comes off income, never allowance
  irregular  shocks; never averaged into a rate           — handled by the 自己造成/無法避免 rules
  skip       not spending at all                          — invisible

Refunds and reimbursements are NOT their own category. A refund carries the category
of the charge it reverses and a positive amount, so every bucket sum nets itself
without a single special case downstream. 41% of Momo's Amazon comes back; gross
totals were lying by hundreds of dollars a month.
"""
from __future__ import annotations

import re

FIXED = "fixed"
FLEX = "flex"
WANT = "want"
WORK = "work"
IRREGULAR = "irregular"
SKIP = "skip"

TREATMENT_LABEL = {
    FIXED: "固定",
    FLEX: "彈性",
    WANT: "想要",
    WORK: "工作",
    IRREGULAR: "不規則",
    SKIP: "不算支出",
}

# id -> (中文名, treatment, 一句話說明)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    # ── 固定 ──────────────────────────────────────────────────────────
    "rent":       ("房租",       FIXED, "每月給媽媽的 $1,000"),
    "gas":        ("加油",       FIXED, "工作要開車，躲不掉"),
    "subs":       ("訂閱工具",   FIXED, "Adobe、Claude、YouTube、iCloud、Railway"),
    "insurance":  ("保險",       FIXED, "GEICO，半年一期"),
    "phone":      ("電話",       FIXED, "Ultra Mobile，半年一期"),

    # ── 彈性 ──────────────────────────────────────────────────────────
    "food":       ("食",         FLEX,  "買菜跟外食算同一桶，你自己決定怎麼配"),
    "snacks":     ("零食飲料甜點", FLEX, "咖啡、手搖、甜點"),
    "fun":        ("娛樂出去玩", FLEX,  "電影、展覽、活動"),
    "household":  ("日常必需品", FLEX,  "衛生紙那類家用消耗品"),
    "transit":    ("交通雜支",   FLEX,  "停車、過路費、偶爾的叫車"),
    "pets":       ("寵物",       FLEX,  "飼料、用品（看醫生算不規則）"),
    "gifts":      ("送禮",       FLEX,  "生日、節日、家人"),

    # ── 想要 ──────────────────────────────────────────────────────────
    "want":       ("想要嗜好",   WANT,  "衣服、指甲油、娃娃、化妝品"),
    "shopping":   ("購物未分類", WANT,  "Amazon/Target 這種一次買一堆的，先當想要，等你分"),

    # ── 工作 ──────────────────────────────────────────────────────────
    "work":       ("工作用品",   WORK,  "美術用品、器材；要標可不可報帳"),

    # ── 不規則 ────────────────────────────────────────────────────────
    "travel":     ("旅行",       IRREGULAR, "要標誰付錢"),
    "fees":       ("證件規費",   IRREGULAR, "DMV、移民、法院"),
    "car":        ("車輛維修",   IRREGULAR, "保養約到 2028，所以只剩修車"),
    "health":     ("醫療",       IRREGULAR, "台灣看病免費，這裡出現就是意外"),
    "fines":      ("罰單違規",   IRREGULAR, "預設算「自己造成」"),

    # ── 不算支出 ──────────────────────────────────────────────────────
    "transfer":   ("轉帳還卡費", SKIP,  "錢在自己戶頭之間搬家"),
    "tax":        ("稅金",       SKIP,  "本來就不是你的錢"),
}

#: display order — grouped by treatment, matching the way she talks about them
ORDER = list(CATEGORIES)

# Which of the seven chart hues each category wears. Assigned deliberately rather than by
# position, because position modulo seven put 旅行 and 加油 on the same blue — and those
# two are both in Momo's top five, so they were charted together and indistinguishable.
# Colour follows the entity: a category keeps its hue on every page, forever.
PALETTE_SLOT: dict[str, int] = {
    "food": 0, "shopping": 1, "travel": 2, "gas": 3, "insurance": 4, "want": 5, "subs": 6,
    "snacks": 2, "fun": 3, "household": 4, "transit": 5, "pets": 6, "gifts": 0,
    "rent": 4, "phone": 5, "work": 6,
    "fees": 0, "car": 1, "health": 2, "fines": 3,
    "transfer": 6, "tax": 6,
}

ALL = set(CATEGORIES)


def label(cat: str | None) -> str:
    """中文 name for a category id (falls back to the raw id)."""
    if not cat:
        return "未分類"
    row = CATEGORIES.get(cat)
    return row[0] if row else cat


def treatment(cat: str | None) -> str | None:
    row = CATEGORIES.get(cat or "")
    return row[1] if row else None


def note(cat: str | None) -> str:
    row = CATEGORIES.get(cat or "")
    return row[2] if row else ""


def of_treatment(tag: str) -> list[str]:
    return [c for c, (_, t, _) in CATEGORIES.items() if t == tag]


def in_allowance(cat: str | None) -> bool:
    """Does this eat the half-month allowance? Only the decisions Momo actually makes."""
    return treatment(cat) in (FLEX, WANT)


def is_fixed(cat: str | None) -> bool:
    return treatment(cat) == FIXED


def is_work(cat: str | None) -> bool:
    return treatment(cat) == WORK


def is_irregular(cat: str | None) -> bool:
    return treatment(cat) == IRREGULAR


def is_skip(cat: str | None) -> bool:
    return treatment(cat) == SKIP


def counts_as_spend(cat: str | None) -> bool:
    """Everything except pure money-movement. Uncategorized still counts — silence
    is not a discount."""
    return not is_skip(cat)


# ── the old names, so 19 months of history survives the rename ────────────────
LEGACY: dict[str, str] = {
    "Food & Groceries": "food",
    "Eating Out": "food",
    "Coffee": "snacks",
    "Rideshare & Transit": "transit",
    "Gas": "gas",
    "Subscriptions": "subs",
    "Shopping": "shopping",
    "Beauty": "want",
    "Pets": "pets",
    "Art & Work Supplies": "work",
    "Health": "health",
    "Fun & Going Out": "fun",
    "Travel": "travel",
    "Gifts": "gifts",
    "Life Admin": "fees",
    "Transfers/Ignore": "transfer",
    "Income": None,  # income is not a spending category any more
}

# "Rent & Utilities" held four different real-world things (rent, phone, insurance,
# a dead storage unit), so it can't be mapped by name alone — resolved by rule below.
_RENT_UTIL_SPLIT = "Rent & Utilities"


def from_legacy(old: str | None, desc: str = "") -> str | None:
    """Translate a pre-taxonomy category. Ambiguous buckets are re-guessed from the
    description rather than mapped blindly."""
    if old in (None, ""):
        return None
    if old in ALL:
        return old  # already migrated
    if old == _RENT_UTIL_SPLIT:
        return guess(desc) or "household"
    if old == "Shopping":
        return guess(desc) or "shopping"
    return LEGACY.get(old)


# ── keyword rules ─────────────────────────────────────────────────────────────
# Order matters: most specific first. Anything not matched stays None and lands in
# the labeling inbox, which is the honest outcome.
_RULES: list[tuple[str, str]] = [
    # 固定 — brand-exact, must beat the generic rules below
    (r"ultra ?mobile", "phone"),
    (r"geico|state farm|progressive|allstate|mercury ins", "insurance"),
    (r"adobe|midjourney|\bxai\b|\bgrok\b|openai|chatgpt|anthropic|claude\b|comfy\.org|"
     r"notion labs|usemotion|visualcrossing|pephop|icloud|apple\.com/bill|google one|"
     r"google storage|railway|vercel|netlify|github|linear\.app|figma", "subs"),
    (r"netflix|spotify|crunchyroll|lezhin|patreon|youtube|hbo|disney ?\+|hulu", "subs"),
    (r"arco|shell oil|chevron|exxon|sinclair|mobil|speedway|gasoline|\bfuel\b|\b76\b|"
     r"rocket \d|\bampm\b|circle k", "gas"),
    (r"\brent\b|zelle.*(mom|媽)|(mom|媽).*zelle", "rent"),

    # 不算支出 — money movement, catch before anything else can claim it
    (r"payment thank you|autopay|auto ?-? ?pay|online payment|card payment|epay|"
     r"\bpymt\b|ach (payment|pmt)|internal transfer|online transfer|acct (transfer|xfer)|"
     r"deposit sweep|intra ?-? ?day|banklink ach|goldman sachs bank|\bgsbank\b", "transfer"),
    (r"\birs\b|usataxpymt|eftps|franchise tax bo|\bca ftb\b|\bftb\b.*(pmt|payment)", "tax"),

    # 不規則
    (r"\bdmv\b|uscis|passport|\bcourt\b|sup ctr|city of |county of |notary", "fees"),
    (r"citation|parking ticket|\bpticket|red light|toll violation|late fee|overdraft", "fines"),
    (r"lexus|toyota|honda|firestone|pep boys|jiffy lube|midas|autozone|o'?reilly|"
     r"smog|body shop|collision|tire", "car"),
    (r"\bcvs\b|walgreens|rite aid|pharmacy|urgent care|dental|dentist|optometr|"
     r"medical|clinic|hospital", "health"),
    # Anything bought abroad is trip spending — that's how Momo budgets a trip, as one
    # lump, not as "clothing" that happened to be in Paris.
    (r"airbnb|united air|starlux|eva air|china air|delta air|american air|\bhotel\b|"
     r"hostel|\btwn\b|taipei|hsinchu|taoyuan|kaohsiung|taichung|\bcdg\b|"
     r"airport|airline|expedia|booking\.com|\bhtl\b|\bunited\b.{0,40}(jefferson|air)|"
     r"\bfra\b|frafra|idffra|\bparis\b|bordeaux|\bfrance\b|\bjpn\b|\bkor\b", "travel"),

    # 工作
    (r"blick|michaels|joann|home ?depot|lowes|harbor freight|staples|1 hour photo|"
     r"printing|sharegrid|ups store|fedex office|bloom connection|flower market|"
     r"st\.? vincent|prop|set dress|home center|gumroad|dataland", "work"),

    # 彈性 — 食
    (r"99 ranch|nijiya|weee|\byami\b|instacart|trader joe|costco|ralphs|vons|albertsons|"
     r"safeway|sprouts|gelsons|marukai|mitsuwa|tokyo central|toyko central|h ?mart|"
     r"whole ?f(oo)?ds|sheng kee|super ?market|grocer", "food"),
    (r"hungrypanda|uber ?\*?eats|grubhub|doordash|postmates|caviar|seamless", "food"),
    (r"mcdonald|popeyes|jollibee|in-?n-?out|raising canes|chipotle|kfc\b|taco bell|"
     r"wingstop|shake shack|chick-?fil|panda express|jack in the box|churchs chicken|"
     r"7-?eleven", "food"),
    (r"tst[\* ]|dumpling|kitchen|restaur|ramen|sushi|izakaya|bbq|grill|pho\b|noodle|"
     r"hot ?pot|szechuan|sichuan|dim ?sum|bistro|thai|pizza|taco|burger|chicken|tofu|"
     r"gopchang|jangguk|poke|deli\b|eatery|diner|seafood|shabu|curry|udon|soba|yakitori|"
     r"katsu|wings|bao\b|congee|porridge|malatang|skewer|banh|kbbq|soondubu|kopitiam|"
     r"uep\*|trattoria|osteria|ristor|dining|dinning|sinbala|guiji|ipoh|caf[eé]\b|"
     r"sun nong|dan sung|golden leaf|red rock|himmel haus|lus garden|\bgarden cafe|"
     r"boiling point|half and half|jangteo|myung|tofu house|gogi|jjigae", "food"),

    # 彈性 — 零食飲料甜點
    (r"starbucks|coffee|espresso|latte|boba|sharetea|gong ?cha|yifang|milk ?tea|heytea|"
     r"tp ?tea|machi|tiger sugar|kung fu tea|sunright|meet fresh|chagee|molly tea|chicha|"
     r"wushiland|pot of cha|dessert|ice ?cream|creamery|\btea\b|philz|blue bottle|"
     r"bakery|baguette|85c|jj bakery|portos|beard papa|snack|sencha|matcha|donut|"
     r"cookie|candy", "snacks"),

    # 彈性 — 其他
    (r"petco|petsmart|chewy|pet food|pet center|pet shop", "pets"),
    (r"steam|playstation|nintendo|\bamc\b|regal|cinemark|cinema|cinepolis|laemmle|"
     r"museum|ticketmaster|\btm \*|ticket tailor|eventbrite|axs\b|brewery|brewing|"
     r"tavern|karaoke|\bktv\b|arcade|bowling|concert", "fun"),
    (r"parking|toll ?road|pnm\*tca|frogparking|\blaz \b|metro\b|\blyft\b|\buber\b|"
     r"youbike|transit|amtrak", "transit"),
    (r"public storage|extra space|u-?haul", "household"),

    # 想要 — the hobby lever
    (r"uniqlo|zara\b|h&m|hm\.com|free people|ross stores|goodwill|windsor|hollister|"
     r"american eagle|lovisa|casetify|vintage|thrift|urban outfit|brandy melville|"
     r"depop|poshmark|figs corner|mango\b|shein|aritzia", "want"),
    (r"sephora|ulta\b|olive young|sally beauty|hair salon|beauty salon|\bnail|barber|"
     r"superbcut|glossier|makeup|cosmetic|rituals\b", "want"),
    (r"plush|pop ?mart|kidrobot|gundam|figure|hobby|craft store|etsy|fanatics|"
     r"bookstore|\bbooks\b|record shop|vinyl", "want"),

    # 一次買一堆的通路 — deliberately last, and deliberately vague
    (r"amazon|amzn|target|walmart|\bebay\b|daiso|ikea|bed bath|marshalls|tj ?maxx|"
     r"big lots|dollar tree|apple store", "shopping"),
]

_COMPILED = [(re.compile(p), c) for p, c in _RULES]


def guess(desc: str) -> str | None:
    """Cheap keyword guess at ingest time. None means 'ask Momo' — which is a valid,
    honest answer, not a failure."""
    m = (desc or "").lower()
    for rx, cat in _COMPILED:
        if rx.search(m):
            return cat
    return None


def is_transfer(desc: str) -> bool:
    return guess(desc) in ("transfer", "tax")


# Payment-processor and filler prefixes that sit in front of the real merchant name.
_BRAND_STOPWORDS = {
    "the", "sq", "tst", "uep", "sp", "py", "fd", "cke", "ckt", "sumup", "ptr",
    "wl", "paypal", "pp", "tm", "snack", "wpy", "ib", "pos", "dd", "gp", "sqc",
}


def brand_key(desc: str) -> str:
    """A coarse merchant identity — 'AMAZON MKTPL*2B9ZI1JM3440 Terry Ave N' and
    'AMAZON MKTPLACE PMTS 440 Terry Ave N' are the same shop, but their full
    descriptions share almost nothing, so :func:`merchant_key` can't see it.

    Used only to pair a refund with the purchase it reverses, where being coarse is
    the point: two charges at the same shop virtually always share a category."""
    s = re.sub(r"\(return\)", " ", (desc or "").lower())
    for tok in re.split(r"[^a-z0-9一-鿿]+", s):
        if not tok or any(ch.isdigit() for ch in tok):
            continue
        if tok in _BRAND_STOPWORDS or len(tok) < 3:
            continue
        return tok
    return merchant_key(desc)


#: the statement literally says so — Apple prints "(RETURN)" on reversed charges
_RETURN_MARK = re.compile(r"\(return\)|\brefund(ed)?\b|\breturn(ed)? to\b")


def looks_like_return(desc: str) -> bool:
    return bool(_RETURN_MARK.search((desc or "").lower()))


def merchant_key(desc: str) -> str:
    """Stable key for merchant memory + dedup. Drops digits and punctuation so
    'WHOLEFOODS #382' and 'Whole Foods' match, but keeps names distinct so
    'ZELLE FROM JOHN' ≠ 'ZELLE FROM MARY'."""
    s = (desc or "").lower()
    s = re.sub(r"[^a-z一-鿿]+", "", s)
    return s[:120] or "unknown"


# ── money coming in ───────────────────────────────────────────────────────────
# Not every deposit is pay. Treating a friend's Zelle as income inflated the budget;
# treating a production's reimbursement as "not income" deleted the offset to a
# purchase Momo had already been charged for. Three kinds, three behaviours.
PAY = "pay"                    # real work income — drives the budget, reserves tax
REIMBURSE_WORK = "reimburse_work"    # production paying back a purchase Momo fronted
REIMBURSE_FAMILY = "reimburse_family"  # parents covering a car repair, a flight
PERSONAL = "personal"          # friends splitting a bill — invisible
REFUND = "refund"              # a merchant giving money back

INFLOW_LABEL = {
    PAY: "工作收入",
    REIMBURSE_WORK: "劇組報帳",
    REIMBURSE_FAMILY: "家裡出的",
    PERSONAL: "朋友還錢",
    REFUND: "退貨退款",
}

#: inflow kinds that cancel an earlier charge instead of counting as income
CANCELS_SPEND = (REIMBURSE_WORK, REIMBURSE_FAMILY, REFUND)


def is_income_kind(kind: str | None) -> bool:
    """Only real pay counts as income — and only real pay gets taxed."""
    return kind == PAY
