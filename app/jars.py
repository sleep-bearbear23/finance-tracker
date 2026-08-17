"""有主的錢 — jars. Money that is Momo's but spoken for.

One structure replaces three parallel systems (tax's carve-out, the defended-floor
threshold, sinking funds) and absorbs the season pot. The spending engine subtracts
``spoken_for()`` from deployable cash EXACTLY ONCE — so a dollar can never be defended
twice or zero times. Jars are virtual envelopes over the same bank accounts; no real
money moves.

Design decisions on record (fin/_design-jars-2026-08-17.md):
- Three reserve pots, release rules keyed to the engine's own deficit diagnosis:
  短期應急 (surprises, ask anytime) · 地板 (timing dips only) · 緊急預備金 (structural,
  as a stated plan). Momo named Apple Savings the reserve stack, eyes open that the
  line drops until income lands.
- Tax fills itself; sinking funds drip per period; everything judgment-shaped fills
  only by Momo's allocation at 期末結算.
- No jar ever drains automatically. A balance moves only on Momo's receipted word.
  If cash falls below the sum of the jars, we SAY so (稅 breached last, and loudest)
  instead of pretending the pots are intact.
- Jar-funded charges leave the period line alone: the pot absorbs the surprise,
  not the grocery money.
"""
from __future__ import annotations

import json

from .db import get_kv, set_kv

KEY = "cfg_jars"          # WATCHED — every write becomes a Change row
SEED_FLAG = "jars_seeded_v1"

#: reserve pots protect survival; goal pots hold plans. The ladder scoreboard counts
#: reserves as part of the pile she's standing on; goals are already-spoken-for money.
RESERVE_KINDS = ("contingency", "floor", "emergency")
GOAL_KINDS = ("sinking", "season", "experiment")

#: display/breach order — which pot is effectively being eaten first when cash dips
#: below the spoken-for total. Tax deliberately last: that was never her money.
DRAW_ORDER = ("contingency", "floor", "emergency", "tax")

#: half-month periods per year, for sinking drips
PERIODS_PER_YEAR = 24


# ── store ─────────────────────────────────────────────────────────────────────

async def load(session) -> list[dict]:
    raw = await get_kv(session, KEY)
    if not raw:
        return []
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


async def save(session, jars: list[dict]) -> None:
    await set_kv(session, KEY, json.dumps(jars, ensure_ascii=False))


def get(jars: list[dict], jid: str) -> dict | None:
    return next((j for j in jars if j.get("id") == jid), None)


async def seeded(session) -> bool:
    return (await get_kv(session, SEED_FLAG)) == "1"


# ── the one total ─────────────────────────────────────────────────────────────

async def spoken_for(session, *, tax_outstanding: float,
                     legacy_floor: float | None = None) -> dict:
    """The single spoken-for figure the engine subtracts, plus the per-jar breakdown.

    Pre-seed (jars never created), the stored portion substitutes the legacy defended
    floor — so the window between deploying this code and pressing 重掃歷史 behaves
    exactly like the old engine (tax + rung), rather than briefly reading richer.
    """
    stored = await load(session)
    if not stored and legacy_floor is not None and not await seeded(session):
        stored = [{"id": "floor", "name": "地板", "kind": "floor",
                   "balance": round(max(0.0, legacy_floor), 2), "target": None,
                   "fill": "allocate", "legacy": True}]

    jars = [{
        "id": "tax", "name": "預留的稅", "kind": "tax",
        "balance": round(max(0.0, tax_outstanding), 2),
        "target": round(max(0.0, tax_outstanding), 2),
        "fill": "auto", "computed": True,
    }] + stored

    total = round(sum(float(j.get("balance") or 0.0) for j in jars), 2)
    reserve = round(sum(float(j.get("balance") or 0.0) for j in jars
                        if j.get("kind") in RESERVE_KINDS), 2)
    goals = round(sum(float(j.get("balance") or 0.0) for j in jars
                      if j.get("kind") in GOAL_KINDS), 2)
    return {"total": total, "reserve": reserve, "goals": goals, "jars": jars}


def breach(available_pool: float, sf: dict) -> dict | None:
    """available_pool = spendable − debts. If the pool no longer covers the jars, name
    which pots are effectively being eaten, in DRAW_ORDER, 稅 last and loudest."""
    short = round(sf["total"] - available_pool, 2)
    if short <= 0.01:
        return None
    eaten, remaining = [], short
    by_kind: dict[str, float] = {}
    for j in sf["jars"]:
        by_kind[j["kind"]] = by_kind.get(j["kind"], 0.0) + float(j.get("balance") or 0)
    for kind in DRAW_ORDER:
        if remaining <= 0:
            break
        bal = by_kind.get(kind, 0.0)
        if bal <= 0:
            continue
        bite = min(bal, remaining)
        if bite > 0.01:
            eaten.append({"kind": kind, "amount": round(bite, 2)})
        remaining = round(remaining - bite, 2)
    return {"short": short, "eaten": eaten,
            "tax_breached": any(e["kind"] == "tax" for e in eaten)}


# ── seeding (idempotent; called from run_maintenance with computed inputs) ────

async def seed(session, *, floor_amount: float, gs_balance: float,
               tax_outstanding: float, emergency_target: float,
               season_pot: float, contingency_target: float = 600.0) -> list[str]:
    """Create the stored jars once. Momo's decisions, in arithmetic:

    - 地板 seeds at today's defended rung, so the bridge that existed keeps existing.
    - 緊急預備金 = Apple Savings − 稅 − 地板: her savings account IS the reserve stack;
      the tax and floor carve-outs come out of it first so no dollar is defended twice.
    - Everything else starts empty and fills at 期末結算 (the income tap).
    """
    if await seeded(session):
        return []
    log: list[str] = []
    floor_seed = round(max(0.0, floor_amount), 2)
    emergency_seed = round(max(0.0, gs_balance - max(0.0, tax_outstanding) - floor_seed), 2)

    jars = [
        {"id": "contingency", "name": "短期應急", "kind": "contingency",
         "purpose": "突發的一筆——獸醫、拖吊、臨時的意外", "balance": 0.0,
         "target": round(contingency_target, 2), "fill": "allocate",
         "release": "ask"},
        {"id": "floor", "name": "地板", "kind": "floor",
         "purpose": "錢在路上還沒到的那幾期，撐過去用的", "balance": floor_seed,
         "target": floor_seed or None, "fill": "allocate", "release": "dip"},
        {"id": "emergency", "name": "緊急預備金", "kind": "emergency",
         "purpose": "收入真的斷掉的時候。Apple Savings 就是這一罐",
         "balance": emergency_seed, "target": round(emergency_target, 2),
         "fill": "allocate", "release": "plan"},
        {"id": "dmv", "name": "DMV", "kind": "sinking",
         "purpose": "一年一次的規費", "balance": 0.0, "target": 371.0,
         "fill": "drip", "annual": 371.0, "release": "cost"},
        {"id": "car", "name": "修車", "kind": "sinking",
         "purpose": "車一定會壞，先存著", "balance": 0.0, "target": 1200.0,
         "fill": "drip", "annual": 1200.0, "release": "cost"},
        {"id": "season", "name": "季目標", "kind": "season",
         "purpose": "期末結算分配進來的，這一季存下的", "balance": round(season_pot, 2),
         "target": None, "fill": "tap", "release": "close"},
        {"id": "experiment", "name": "實驗（AI）", "kind": "experiment",
         "purpose": "學東西的預算——罐子的一種，不是目的", "balance": 0.0,
         "target": None, "fill": "allocate", "release": "ask"},
    ]
    await save(session, jars)
    await set_kv(session, SEED_FLAG, "1")

    log.append(f"[jars] 建了 7 個罐子。地板 ${floor_seed:,.2f}（今天守住的水位，原樣搬進來）")
    log.append(f"[jars] 緊急預備金 ${emergency_seed:,.2f}"
               f" = Apple Savings ${gs_balance:,.2f} − 稅 ${max(0.0, tax_outstanding):,.2f}"
               f" − 地板 ${floor_seed:,.2f}（你說的：Apple Savings 就是預備金）")
    if season_pot:
        log.append(f"[jars] 季目標接收了原本的 cfg_season_pot ${season_pot:,.2f}")
    log.append("[jars] 短期應急、DMV、修車、實驗都從 $0 開始——結算的時候由你分配；"
               "DMV 跟修車從這期開始每期自動滴 $15.46 / $50.00")
    return log


# ── sinking drips (idempotent per period key) ─────────────────────────────────

async def accrue(session, period_key: str) -> list[str]:
    """Advance each drip-fill jar by one period's share, once per period."""
    jars = await load(session)
    if not jars:
        return []
    log, dirty = [], False
    for j in jars:
        if j.get("fill") != "drip" or not j.get("annual"):
            continue
        if j.get("last_accrued") == period_key:
            continue
        step = round(float(j["annual"]) / PERIODS_PER_YEAR, 2)
        target = j.get("target")
        new_bal = round(float(j.get("balance") or 0.0) + step, 2)
        if target:
            new_bal = min(new_bal, float(target))  # a full pot stops dripping
        if abs(new_bal - float(j.get("balance") or 0.0)) > 0.001:
            j["balance"] = new_bal
            log.append(f"[jars] {j['name']} +${step:,.2f} → ${new_bal:,.2f}")
        j["last_accrued"] = period_key
        dirty = True
    if dirty:
        await save(session, jars)
    return log


# ── moves (every caller wraps these in changelog.watching) ────────────────────

async def allocate(session, jid: str, amount: float) -> dict:
    """Put money into a jar — 結算分配, or any time Momo says so."""
    if amount <= 0:
        return {"ok": False, "error": "金額要是正的"}
    jars = await load(session)
    j = get(jars, jid)
    if j is None:
        return {"ok": False, "error": f"沒有叫「{jid}」的罐子"}
    j["balance"] = round(float(j.get("balance") or 0.0) + amount, 2)
    await save(session, jars)
    return {"ok": True, "jar": j["name"], "balance": j["balance"],
            "receipt": f"{j['name']} +${amount:,.2f} → ${j['balance']:,.2f}"}


async def draw(session, jid: str, amount: float, *, deficit_kind: str | None = None,
               dip_active: bool = False, plan: str | None = None) -> dict:
    """Take money out — enforcing each pot's release rule SERVER-SIDE, not by manners.

    - contingency / experiment / season: ask anytime.
    - sinking: when the real cost arrives.
    - floor: only when the period is genuinely underwater on TIMING (dip mode, money is
      coming). A structural hole is not a bridge situation.
    - emergency: only for a STRUCTURAL deficit, and only with a stated plan
      (per-period amount × periods). Never a casual tap.
    - tax: never drawable here. Paying the IRS happens in the ledger, not by wishing.
    """
    if jid == "tax":
        return {"ok": False, "error": "稅的錢不能動——那本來就不是你的。要繳稅是繳給國稅局，不是從罐子拿。"}
    if amount <= 0:
        return {"ok": False, "error": "金額要是正的"}
    jars = await load(session)
    j = get(jars, jid)
    if j is None:
        return {"ok": False, "error": f"沒有叫「{jid}」的罐子"}

    rule = j.get("release") or "ask"
    if rule == "dip" and not (dip_active and (deficit_kind or "timing") == "timing"):
        return {"ok": False, "error": "地板只有在這期真的撐不住、而且錢確定在路上（timing）的時候才能動。"
                                      "現在不是那個狀況。"}
    if rule == "plan":
        if deficit_kind != "structural":
            return {"ok": False, "error": "緊急預備金是收入斷掉時用的。現在的缺口不是結構性的，先看地板或應急。"}
        if not plan:
            return {"ok": False, "error": "動預備金要先講清楚計畫：每期拿多少、撐幾期。講了我才動。"}

    bal = float(j.get("balance") or 0.0)
    if amount > bal + 0.001:
        return {"ok": False, "error": f"{j['name']} 只有 ${bal:,.2f}，拿不出 ${amount:,.2f}。最多 ${bal:,.2f}。"}
    j["balance"] = round(bal - amount, 2)
    await save(session, jars)
    return {"ok": True, "jar": j["name"], "balance": j["balance"],
            "receipt": f"{j['name']} −${amount:,.2f} → 剩 ${j['balance']:,.2f}"
                       + (f"（計畫：{plan}）" if plan else "")}


async def fund_charge(session, txn, jid: str = "contingency") -> dict:
    """Pay a surprise charge FROM a jar: pot down, period line untouched.

    The transaction keeps its true category (a vet bill is still 寵物) but carries
    ``jar_id``, and every spend calculation skips jar-funded rows — the surprise is
    absorbed by money that was standing by for surprises, not by grocery money.
    """
    amount = abs(float(txn.amount))
    out = await draw(session, jid, amount)
    if not out.get("ok"):
        return out
    txn.jar_id = jid
    out["receipt"] += f"，這筆 ${amount:,.2f} 不吃這期的額度"
    return out
