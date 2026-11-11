"""The pre-registered strategy comparison, as amended.

Rules: docs/multiple-comparisons-policy.md — Option C (ratified 2026-08-27) and
Amendment 1 (ratified 2026-11-10, in force for data settled after
2026-11-11T03:30:00Z). This script only applies them; it decides nothing the
text does not.

What it computes, per market:

- PAIRING. A beats B on a day if A's P&L is higher by at least one cent (deltas
  rounded to cents, |delta| < 0.01 is a tie and is excluded — rule A).
- EXPLORATORY comparisons (everything except the one below). Only days whose
  rows were BOTH settled after the amendment count (clause 6). The family is
  formed per market (clause 1): each "shadow S beats the primary" (rule R1) and
  each comparison rule R7 needs ("the primary beats S", "shadow T beats S"),
  provided it meets its precondition (R1: 30 non-tied days; R7: 30 non-tied and
  60 shared days — clause 7). Holm step-down inside the family at a per-look
  threshold of 0.05/12 (clause 4). A market whose primary does not commit has no
  evaluation (clause 3).
- The ONE CONFIRMATORY comparison: climatology vs persistence in Spain, on
  exactly its first 90 non-tied days since 2026-08-01, at 0.05, decided once
  (clauses 2 and 5). It is never a member of a family.
- p_eff = max(p_iid, p_boot): the iid sign test floored against the frozen
  moving-block-bootstrap table (Data/calibration/sign_bar.json, n = 30..90).
  Beyond the table the plain sign test applies. The file must be byte-identical
  to the one frozen on 2026-08-27 (clause 8) or no verdict is printed.

WHAT IT PRINTS. By default: wins, ties and losses and how far each comparison is
from a decision look — no p-value and no verdict, because a comparison is
decided only at a month-end review (clause 4). With --decision-look (the
month-end review and the referee): the family, k, the thresholds, p_eff and the
R1/R7 verdicts.

stdlib only: it READS the frozen calibration; producing it needs
scripts/calibrate_sign_bar.py (numpy/statsmodels/arch), run once.

Run: uv run python scripts/compare_strategies.py                      # counts, all markets
     uv run python scripts/compare_strategies.py --market es
     uv run python scripts/compare_strategies.py --decision-look [--as-of ISO]
     uv run python scripts/compare_strategies.py A B [--market es]    # one pair, counts
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from talea import loop  # noqa: E402
from talea.markets import MARKETS  # noqa: E402

DATA = ROOT / "Data"
CALIBRATION = DATA / "calibration" / "sign_bar.json"
# The file frozen on 2026-08-27 (commit e3e1e14); recorded in the policy,
# Amendment 1 clause 8. A different file means no verdict.
CALIBRATION_SHA256 = "024cca63c588211f5547d81e7d9ae27f2753e9dec4135ec1729b4bb68dd04044"
TIE_EPS = 0.01

PERSISTENCE = "battery-2h2h-persistence"
CLIMATOLOGY = "battery-2h2h-climatology"
# Amendment 1: the ratification instant; a row counts for an exploratory
# comparison only if it was settled AFTER it (clause 6).
AMENDMENT_1_AT = datetime(2026, 11, 11, 3, 30, tzinfo=timezone.utc)
LOOK_ALPHA = 0.05 / 12          # per decision look, before Holm (clause 4)
N_MIN = 30                      # non-tied days (clause 4)
R7_SHARED_MIN = 60              # shared settled days, R7 comparisons only (clause 7)
# The single a-priori comparison (registered 2026-08-01), clauses 2 and 5.
CONFIRMATORY = {"market": "es", "a": CLIMATOLOGY, "b": PERSISTENCE,
                "since": "2026-08-01", "n": 90, "alpha": 0.05}


# ---- the statistics -----------------------------------------------------------

def sign_test_p(wins: int, n: int) -> float:
    """One-sided binomial P(X >= wins | n, 0.5) — the iid sign test (p_iid)."""
    return sum(comb(n, i) for i in range(wins, n + 1)) / 2 ** n


def load_calibration() -> dict | None:
    return json.loads(CALIBRATION.read_text()) if CALIBRATION.exists() else None


def calibration_problem() -> str | None:
    """Why the calibration cannot be used for a verdict, or None if it can.
    Clause 8: it must be the file frozen on 2026-08-27, byte for byte."""
    if not CALIBRATION.exists():
        return f"{CALIBRATION.name} is missing"
    digest = hashlib.sha256(CALIBRATION.read_bytes()).hexdigest()
    if digest != CALIBRATION_SHA256:
        return (f"{CALIBRATION.name} is not the frozen file (sha256 {digest[:12]}…, "
                f"expected {CALIBRATION_SHA256[:12]}…)")
    if not (load_calibration() or {}).get("valid"):
        return (f"{CALIBRATION.name} is marked not valid; the amendment does not say "
                f"what the per-look threshold is in the fallback regime")
    return None


def p_effective(cal: dict | None, wins: int, n: int) -> tuple[float, str]:
    """p_eff = max(p_iid, p_boot): the frozen bootstrap can only harden the bar.
    Where the table has no row for n (it covers 30..90) the plain sign test
    applies, as the amendment states."""
    p_iid = sign_test_p(wins, n)
    table = ((cal or {}).get("survival") or {}).get(str(n))
    if table is not None and 0 <= wins < len(table):
        p_boot = table[wins]
        return max(p_iid, p_boot), ("p_boot" if p_boot >= p_iid else "p_iid-floor")
    return p_iid, "p_iid, no table row"


# ---- rows and pairing ---------------------------------------------------------

def strategy_id(row: dict) -> str:
    """A strategy is (name, version). Version 1 goes by the bare name."""
    v = str(row.get("strategy_version") or "1")
    return row["strategy"] if v == "1" else f"{row['strategy']}@v{v}"


def settled_at(row: dict) -> datetime | None:
    ts = row.get("settled_at")
    if not ts:
        return None
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _counts(row: dict, after: datetime | None, as_of: datetime | None) -> bool:
    """Does this settled row count? `after`: settled strictly later than this
    (a row with no settlement time cannot show that, so it does not count).
    `as_of`: not settled later than this (reproduces an earlier look)."""
    if "pnl_eur" not in row:
        return False
    t = settled_at(row)
    if after is not None and (t is None or t <= after):
        return False
    return not (as_of is not None and t is not None and t > as_of)


def pair(rows: list[dict], a: str, b: str, *, after: datetime | None = None,
         as_of: datetime | None = None, since_target: str | None = None,
         first_n: int | None = None) -> dict:
    """A vs B over the days on which BOTH have a row that counts. `first_n`
    stops at the first_n-th non-tied day (the confirmatory terminal look)."""
    by_day: dict[str, dict[str, dict]] = {}
    for r in rows:
        s = strategy_id(r)
        if s in (a, b) and _counts(r, after, as_of) \
                and not (since_target and r["target"] < since_target):
            by_day.setdefault(r["target"], {})[s] = r
    wins = losses = ties = 0
    total = pnl_a = pnl_b = oracle_a = oracle_b = 0.0
    days: list[str] = []
    for day in sorted(by_day):
        v = by_day[day]
        if a not in v or b not in v:
            continue
        delta = round(v[a]["pnl_eur"] - v[b]["pnl_eur"], 2)
        wins += delta >= TIE_EPS
        losses += delta <= -TIE_EPS
        ties += abs(delta) < TIE_EPS
        total += delta
        days.append(day)
        oa, ob = v[a].get("oracle_pnl_eur"), v[b].get("oracle_pnl_eur")
        if oa is not None and ob is not None:      # pooled capture: clause 7
            pnl_a += v[a]["pnl_eur"]; oracle_a += oa
            pnl_b += v[b]["pnl_eur"]; oracle_b += ob
        if first_n is not None and wins + losses >= first_n:
            break
    return {"a": a, "b": b, "shared": len(days), "wins": wins, "losses": losses,
            "ties": ties, "n": wins + losses, "total": total,
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "pooled_capture_a": pnl_a / oracle_a if oracle_a else None,
            "pooled_capture_b": pnl_b / oracle_b if oracle_b else None}


# ---- a market's look ----------------------------------------------------------

def primary_strategy() -> str:
    """Clause 7: the strategy registered as primary at the time of the look."""
    return loop.STRATEGIES[0]["strategy"]


def is_confirmatory(slug: str, a: str, b: str) -> bool:
    return (slug, a, b) == (CONFIRMATORY["market"], CONFIRMATORY["a"], CONFIRMATORY["b"])


def market_look(slug: str, rows: list[dict], cal: dict | None, *,
                as_of: datetime | None = None, primary: str | None = None) -> dict:
    """Everything Amendment 1 says about one market's exploratory comparisons:
    the comparisons, which are in the family, the Holm thresholds, and what
    R1 and R7 conclude. A caller that is not a decision look must show only the
    counts (see the module docstring)."""
    primary = primary or primary_strategy()
    names = sorted({strategy_id(r) for r in rows if "pnl_eur" in r})
    shadows = [s for s in names if s != primary]
    out = {"market": slug, "primary": primary, "shadows": shadows,
           "comparisons": [], "k": 0, "r1": {}, "r7": {}, "evaluated": primary in names}
    if not out["evaluated"]:         # clause 3: the primary does not commit here
        return out

    specs = []
    for s in shadows:
        specs.append(("R1", s, primary))
        specs.append(("R7", primary, s))
        specs += [("R7", t, s) for t in shadows if t != s]
    comps = []
    for rule, a, b in specs:
        c = {**pair(rows, a, b, after=AMENDMENT_1_AT, as_of=as_of), "rule": rule,
             "confirmatory": is_confirmatory(slug, a, b), "significant": False,
             "p_eff": None, "p_kind": None, "threshold": None}
        c["eligible"] = (not c["confirmatory"] and c["n"] >= N_MIN
                         and (rule == "R1" or c["shared"] >= R7_SHARED_MIN))
        if c["eligible"]:
            c["p_eff"], c["p_kind"] = p_effective(cal, c["wins"], c["n"])
        comps.append(c)

    # Holm step-down inside this market's family, at the per-look threshold.
    family = sorted((c for c in comps if c["eligible"]),
                    key=lambda c: (c["p_eff"], c["a"], c["b"]))
    k, stopped = len(family), False
    for j, c in enumerate(family):
        c["threshold"] = LOOK_ALPHA / (k - j)
        if not stopped and c["p_eff"] <= c["threshold"]:
            c["significant"] = True
        else:
            stopped = True
    out["comparisons"], out["k"] = comps, k

    def find(rule, a, b):
        return next(c for c in comps if (c["rule"], c["a"], c["b"]) == (rule, a, b))

    for s in shadows:
        r1 = find("R1", s, primary)
        if r1["confirmatory"]:
            out["r1"][s] = "decided by the confirmatory comparison, not here"
        elif not r1["eligible"]:
            out["r1"][s] = f"not at its precondition ({r1['n']} of {N_MIN} non-tied days)"
        elif not r1["significant"]:
            out["r1"][s] = "sign test not met"
        elif (r1["pooled_capture_a"] is not None and r1["pooled_capture_b"] is not None
              and r1["pooled_capture_a"] >= r1["pooled_capture_b"]):
            out["r1"][s] = "BAR MET"
        else:
            out["r1"][s] = "sign test met, pooled capture below the primary's: bar not met"

        vs_primary = find("R7", primary, s)
        vs_shadows = [find("R7", t, s) for t in shadows if t != s]
        if vs_primary["significant"] and any(c["significant"] for c in vs_shadows):
            out["r7"][s] = "DOMINATED"
        elif not vs_primary["eligible"]:
            out["r7"][s] = (f"not at its precondition ({vs_primary['shared']} of "
                            f"{R7_SHARED_MIN} shared days, {vs_primary['n']} of {N_MIN} non-tied)")
        else:
            out["r7"][s] = "not dominated"
    return out


def confirmatory_look(rows: list[dict], cal: dict | None, *,
                      as_of: datetime | None = None) -> dict:
    """Clause 5: decided once, on exactly the first 90 non-tied shared days
    since 2026-08-01, at 0.05 with p_eff. Before the 90th day there is no
    verdict; after it the verdict never changes, whatever is settled later."""
    c = CONFIRMATORY
    so_far = pair(rows, c["a"], c["b"], as_of=as_of, since_target=c["since"])
    out = {**so_far, "reached": so_far["n"] >= c["n"], "target_n": c["n"],
           "alpha": c["alpha"], "p_eff": None, "p_kind": None, "met": None}
    if out["reached"]:
        first = pair(rows, c["a"], c["b"], as_of=as_of, since_target=c["since"],
                     first_n=c["n"])
        p_eff, kind = p_effective(cal, first["wins"], first["n"])
        out.update({**first, "reached": True, "p_eff": p_eff, "p_kind": kind,
                    "met": p_eff < c["alpha"], "days_so_far": so_far["n"]})
    return out


# ---- printing -----------------------------------------------------------------

def short(name: str) -> str:
    return name.replace("battery-2h2h-", "")


def _wtl(c: dict) -> str:
    return f"{c['wins']}W-{c['ties']}T-{c['losses']}L"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def render_counts(slug: str, look: dict, conf: dict | None) -> list[str]:
    """Between decision looks: counts and distance to a look. No p, no verdict."""
    lines = [f"{slug.upper()}  primary: {short(look['primary'])}"]
    if not look["evaluated"]:
        return lines + ["  the primary does not commit in this market: no R1 or R7 "
                        "evaluation until it does (Amendment 1, clause 3)"]
    for c in look["comparisons"]:
        if c["rule"] != "R1":
            continue
        if c["confirmatory"] and conf is not None:
            lines.append(
                f"  {short(c['a']):12} vs primary  confirmatory, since "
                f"{CONFIRMATORY['since']}: {_wtl(conf)} — {min(conf['n'], conf['target_n'])} "
                f"of {conf['target_n']} non-tied days toward its one terminal look")
        else:
            lines.append(
                f"  {short(c['a']):12} vs primary  since the amendment: {_wtl(c)} — "
                f"{c['n']} of {N_MIN} non-tied days toward a decision look")
    return lines


def render_decision(slug: str, look: dict, conf: dict | None) -> list[str]:
    """At a month-end review: the family, k, thresholds, p_eff and verdicts."""
    lines = [f"{slug.upper()}  primary: {short(look['primary'])}"]
    if not look["evaluated"]:
        return lines + ["  the primary does not commit in this market: no R1 or R7 "
                        "evaluation (Amendment 1, clause 3)"]
    family = sorted((c for c in look["comparisons"] if c["eligible"]),
                    key=lambda c: (c["p_eff"], c["a"], c["b"]))
    lines.append(f"  family: k = {look['k']}" + ("" if family else
                 " — no comparison meets its precondition, so nothing is decided"))
    for j, c in enumerate(family, 1):
        lines.append(
            f"    {j}. {c['rule']} {short(c['a'])} beats {short(c['b'])}: n={c['n']} "
            f"({_wtl(c)}, {c['shared']} shared, {c['first_day']}..{c['last_day']}) "
            f"p_eff={c['p_eff']:.6f} ({c['p_kind']}) vs {c['threshold']:.6f} -> "
            f"{'SIGNIFICANT' if c['significant'] else 'not significant'}")
    waiting = [c for c in look["comparisons"] if not c["eligible"] and not c["confirmatory"]]
    if waiting:
        lines.append("  not in the family (precondition not met; counts only):")
        lines += [f"    {c['rule']} {short(c['a'])} beats {short(c['b'])}: {_wtl(c)}, "
                  f"{c['n']} non-tied, {c['shared']} shared" for c in waiting]
    promoted = []
    for s in look["shadows"]:
        r1 = next(c for c in look["comparisons"]
                  if (c["rule"], c["a"]) == ("R1", s))
        cap = (f" (pooled capture {_pct(r1['pooled_capture_a'])} vs "
               f"{_pct(r1['pooled_capture_b'])})" if r1["significant"] else "")
        lines.append(f"  R1 {short(s)}: {look['r1'][s]}{cap}")
        lines.append(f"  R7 {short(s)}: {look['r7'][s]}")
        promoted += [s] if look["r1"][s] == "BAR MET" else []
    if len(promoted) > 1:
        lines.append("  NOTE: more than one shadow meets R1 at this look. The rules do "
                     "not say which is promoted — that is the operator's decision.")
    if conf is not None:
        lines.append(_render_confirmatory(conf))
    return lines


def _render_confirmatory(conf: dict) -> str:
    head = (f"  confirmatory (clause 5) {short(CONFIRMATORY['a'])} beats "
            f"{short(CONFIRMATORY['b'])}, since {CONFIRMATORY['since']}: ")
    if not conf["reached"]:
        return head + (f"{conf['n']} of {conf['target_n']} non-tied days ({_wtl(conf)}) — "
                       f"not at its terminal look; no verdict")
    return head + (
        f"first {conf['target_n']} non-tied days ({conf['first_day']}..{conf['last_day']}, "
        f"{_wtl(conf)}, {conf['shared']} shared) p_eff={conf['p_eff']:.6f} "
        f"({conf['p_kind']}) vs {conf['alpha']} -> "
        f"{'BAR MET' if conf['met'] else 'BAR NOT MET'} — final; pooled capture "
        f"{_pct(conf['pooled_capture_a'])} vs {_pct(conf['pooled_capture_b'])}")


# ---- CLI ----------------------------------------------------------------------

def load_rows(slug: str) -> list[dict] | None:
    path = DATA / slug / "ledger.jsonl"
    if not path.exists():
        return None
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _parse_instant(text: str) -> datetime:
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pair", nargs="*", metavar="STRATEGY",
                    help="two strategy names: show that one pair's counts")
    ap.add_argument("--market", help="one market slug (default: all)")
    ap.add_argument("--decision-look", action="store_true",
                    help="a month-end review: print p_eff, thresholds and verdicts")
    ap.add_argument("--as-of", type=_parse_instant, default=None,
                    help="ignore rows settled after this instant (reproduce a look)")
    ap.add_argument("--panel", action="store_true", help=argparse.SUPPRESS)  # old name
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if len(a.pair) not in (0, 2) or (a.market and a.market not in MARKETS):
        ap.error("give two strategy names or none; --market must be a registered slug")

    if a.pair:
        slug = a.market or CONFIRMATORY["market"]
        rows = load_rows(slug) or []
        ever = pair(rows, *a.pair, as_of=a.as_of)
        since = pair(rows, *a.pair, after=AMENDMENT_1_AT, as_of=a.as_of)
        if not ever["shared"]:
            print(f"no shared settled days between {a.pair[0]} and {a.pair[1]} in {slug}")
            return 1
        print(f"{slug.upper()}  {a.pair[0]} vs {a.pair[1]}")
        print(f"  all settled days: {ever['shared']} shared, {_wtl(ever)}, total delta "
              f"{ever['total']:+.2f}")
        print(f"  since the amendment ({AMENDMENT_1_AT:%Y-%m-%dT%H:%MZ}): "
              f"{since['shared']} shared, {_wtl(since)}")
        print("  counts only: a comparison is decided at a month-end review "
              "(Amendment 1, clause 4)")
        return 0

    cal = load_calibration()
    if a.decision_look:
        problem = calibration_problem()
        if problem:
            print(f"NO VERDICT: {problem}. Stop and tell the operator.")
            return 2
        print("DECISION LOOK — valid only at a month-end review (Amendment 1, clause 4)")
        print(f"calibration: {CALIBRATION.name} is the frozen file "
              f"(sha256 {CALIBRATION_SHA256[:12]}…)")
        print(f"exploratory: rows settled after {AMENDMENT_1_AT:%Y-%m-%dT%H:%MZ}; "
              f"per-look threshold 0.05/12 = {LOOK_ALPHA:.6f} before Holm; "
              f"n >= {N_MIN} (R7: also >= {R7_SHARED_MIN} shared days)")
    else:
        print("Strategy comparisons — counts only. A comparison is decided at a "
              "month-end review (Amendment 1, clause 4).")
    seen: list[datetime] = []
    for slug in ([a.market] if a.market else list(MARKETS)):
        rows = load_rows(slug)
        if rows is None:
            print(f"{slug.upper()}  no ledger in this checkout")
            continue
        seen += [t for t in map(settled_at, rows)
                 if t and (a.as_of is None or t <= a.as_of)]
        look = market_look(slug, rows, cal, as_of=a.as_of)
        conf = (confirmatory_look(rows, cal, as_of=a.as_of)
                if slug == CONFIRMATORY["market"] else None)
        render = render_decision if a.decision_look else render_counts
        print("\n".join(render(slug, look, conf)))
    if seen:
        print(f"data: latest settled row {max(seen).isoformat()} — to reproduce this "
              f"output later, add: --as-of {max(seen).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
