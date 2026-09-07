"""The paper loop: commit-before-truth receipts, settle against published prices.

Adopted rule (the whole point): the metric is MONEY NET OF COSTS on
out-of-sample decisions committed in advance. Every decision is an append-only
receipt written BEFORE its target day's prices exist anywhere in our dataset;
every settlement joins receipt to published truth and appends the realized P&L
to the ledger. Nothing is ever revised.

Strategy v1 (pre-registered; changing it = a new strategy version, old receipts
stand): virtual battery 1 MW / 2 MWh, 85% round-trip, 0.5 EUR/MWh fees. For
target day T, buy 1 MWh in each of the 2 cheapest hours and sell 0.85 MWh in
each of the 2 dearest hours, hours PICKED FROM DAY T-1's profile (persistence
hour-picking — captured 93% of the perfect-foresight spread over the probe's
122 days).

Shadow baseline (pre-registered 2026-08-01): `battery-2h2h-climatology` v1 —
same battery, hours picked from the PER-HOUR MEAN over the complete days in
the trailing 28 (an hour qualifies when at least half those days have it; the
DST-spring day lacks 02). Runs as parallel receipts per invariant 3: same
commit-before-truth tick, own ledger rows, never mixed with v1's. It exists to
(a) complete the baseline panel (persistence vs climatology, the playbook
pair) and (b) exercise the exact shadow-mode path GBM v2 will use. Every
receipt/ledger row is keyed by (target, strategy, strategy_version).

THE LEAK GUARD (load-bearing): a receipt for target T is committed ONLY if no
price for T is present in the dataset. The D+1 auction publishes ~13:15 CET; a
tick that runs after publication refuses to commit (logged as a missed window)
rather than commit with oracle knowledge. A missed day is honest; a leaked
receipt is worthless.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("Europe/Madrid")
# Hard commit deadline (Europe/Madrid clock). The auction publishes ~13:15
# CET; the leak guard is dataset-relative and cannot see prices we failed to
# fetch — an all-fetches-failed tick after publication would otherwise
# commit a receipt timestamped AFTER truth existed publicly (adversarial
# review finding, 2026-08-20). Belt (dataset) + suspenders (clock).
COMMIT_DEADLINE_HOUR = 13


def market_now(tz: ZoneInfo = MARKET_TZ) -> datetime:
    return datetime.now(tz)


def market_today(tz: ZoneInfo = MARKET_TZ) -> date:
    """The market's current delivery date, in its OWN timezone. Receipts and
    settlements are keyed by market-local days, NEVER machine-local ones: the
    loop has run from CEST, EDT, and (for other markets) other zones, so local
    date.today() can disagree with the market day by one."""
    return datetime.now(tz).date()


DATA_DIR = Path(__file__).resolve().parents[2] / "Data"
# ES lives at Data/es/ like every other market (Stage B, 2026-08-28) — no market
# is privileged at the project root. Data/ root holds only shared/project artifacts
# (esios_prices.json deep history, calibration/) + one subdir per market.
ES_DIR = DATA_DIR / "es"
PRICES = ES_DIR / "prices.json"
RECEIPTS = ES_DIR / "receipts.jsonl"
LEDGER = ES_DIR / "ledger.jsonl"

STRATEGY = "battery-2h2h-persistence"          # the PRIMARY strategy (heartbeat key)
STRATEGY_VERSION = "1"
CLIM_WINDOW = 28        # trailing days scanned for the climatology basis
CLIM_MIN_DAYS = 14      # minimum complete days among them, else skip (honest miss)
POWER_MW = 1.0          # per-hour energy per leg (MWh)
N_HOURS = 2             # buy hours = sell hours = 2
RT_EFF = 0.85           # round-trip efficiency (applied to the sell leg)
FEE_EUR_MWH = 0.5       # per MWh moved, both directions
MIN_BASIS_HOURS = 23    # a basis day must be complete (23 on the DST-spring day)
# Backoff after a failed fetch (the 2026-08-04 DNS outage cost a receipt a
# retry would have saved). Last attempt lands ~11:20, still >1.5h before the
# ~13:15 publication — the leak guard, not the retry, remains the commit gate.
FETCH_RETRY_DELAYS_S = (300, 900)


@dataclass(frozen=True)
class Presentation:
    """How a market renders on a public dashboard (owned by the market, READ by
    scripts/render_dashboard.py). Left as defaults for markets with no public
    page — the market-first single source of truth for presentation, replacing
    the hardcoded dict that used to live in render_dashboard (Phase 0)."""
    title: str = ""
    tab_name: str = ""
    tz_label: str = ""
    source: str = ""
    show_gate: bool = False
    # What the receipts are committed BEFORE, in this market's own terms (the
    # page footer's "committed and pushed before <publication>"). Empty = the
    # renderer's generic fallback. Added 2026-09-07 after the auditor found the
    # DE/ERCOT footers quoting Spain's "~13:15 CET" — the renderer's template
    # had ONE hardcoded publication time for every market.
    publication: str = ""
    # A market-specific disclosure rendered verbatim on its public page (e.g.
    # GB: the settlement index is a traded within-day index, not a day-ahead
    # auction, so the persistence primary structurally never commits there).
    # Empty = nothing to disclose.
    note: str = ""


@dataclass(frozen=True)
class Market:
    """A day-ahead market the loop can run. Spain (ES) is the default and uses
    the repo-root Data/ tree; additional markets get Data/<slug>/, their own
    timezone, commit deadline, currency, and fetch client. The strategy panel,
    P&L math, leak/clock guards, and telemetry are all market-agnostic — only
    these parameters change (multi-market build, 2026-08-22).

    The flags below are the SINGLE SOURCE OF TRUTH for what used to be five
    hardcoded slug lists (digest, server_tick, render_dashboard, publish_mirror,
    the ARCHITECTURE table): operational surfaces query the registry and branch
    on these flags instead of hardcoding subsets (Phase 0, 2026-08-27)."""
    slug: str
    tz: ZoneInfo
    deadline_hour: int
    currency: str
    prices_path: Path
    receipts_path: Path
    ledger_path: Path
    fetch: Callable[[date, date], dict[str, float]]
    # Fetch-retry backoff (seconds between attempts). ES uses (300, 900) — a
    # transient outage on the PRIMARY is worth waiting out. SILENT markets
    # get () = fail fast: they run before ES in server_tick.sh, so a stalled
    # silent fetch would delay the primary tick past its deadline.
    fetch_retries: tuple[int, ...] = ()
    primary: bool = False           # drives the heartbeat / the full ES pass
    public: bool = False            # has a public mirror + dashboard
    driver: str = "server"          # "server" (esios-tick) | "actions" (geo-block)
    redistributable: bool = False   # raw prices may be republished
    presentation: Presentation = field(default_factory=Presentation)

    @classmethod
    def make(cls, slug: str, tz: str | ZoneInfo, fetch: Callable, *,
             deadline_hour: int = COMMIT_DEADLINE_HOUR, currency: str = "EUR",
             root: Path | None = None, fetch_retries: tuple[int, ...] = (),
             primary: bool = False, public: bool = False, driver: str = "server",
             redistributable: bool = False,
             presentation: Presentation | None = None) -> "Market":
        d = (root or DATA_DIR) / slug
        return cls(slug, ZoneInfo(tz) if isinstance(tz, str) else tz,
                   deadline_hour, currency, d / "prices.json",
                   d / "receipts.jsonl", d / "ledger.jsonl", fetch,
                   fetch_retries, primary, public, driver, redistributable,
                   presentation or Presentation())


def _default_market() -> Market:
    """Spain on the repo-root Data/ paths. Reads the module globals LIVE so
    tests that monkeypatch loop.PRICES/RECEIPTS/LEDGER keep working, and so
    ES behavior is byte-identical to the pre-parameterization loop.

    The ES fetcher is imported LAZILY here (not at module scope): its client now
    lives at markets/es/fetch.py, and markets/ imports loop — a module-level
    import would be a cycle. This keeps the core free of a module-time dependency
    on any market's fetcher (Stage A of the ES migration, 2026-08-28)."""
    from .markets.es.fetch import fetch_hourly
    return Market("es", MARKET_TZ, COMMIT_DEADLINE_HOUR, "EUR",
                  PRICES, RECEIPTS, LEDGER, fetch_hourly, FETCH_RETRY_DELAYS_S)


# --- storage (dataset of record: hourly prices; append-only receipt/ledger) ---

def load_prices(path: Path | None = None) -> dict[str, float]:
    path = path or PRICES
    if path.exists():
        return {r["ts"]: r["price"] for r in json.loads(path.read_text())}
    return {}


def save_prices(prices: dict[str, float], path: Path | None = None) -> None:
    """Rewrite the dataset of record atomically: write a temp file, fsync it,
    then os.replace (atomic on POSIX). A crash never leaves a half-written
    prices.json — the leak guard reads this file, so a torn one is dangerous."""
    path = path or PRICES
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"ts": k, "price": v} for k, v in sorted(prices.items())]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows))
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _repair_torn_tail(path: Path) -> None:
    """Before appending, remove an interrupted trailing write (a crash after
    the last record's newline but before this one's) so every WHOLE line in the
    append-only file is a committed record. Only the final line can be torn
    (O_APPEND writes don't interleave); it's a complete record iff the file ends
    in '\\n'. The partial bytes are QUARANTINED to <name>.corrupt, never dropped
    silently."""
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n") + 1            # 0 if the whole file is one torn line
    torn = data[cut:]
    with (path.parent / (path.name + ".corrupt")).open("ab") as q:
        q.write(torn + b"\n")
    with path.open("rb+") as fh:
        fh.truncate(cut)
        fh.flush()
        os.fsync(fh.fileno())
    print(f"[esios-paper] quarantined a torn trailing write in {path.name} "
          f"({len(torn)} B -> {path.name}.corrupt); append-only invariant restored")


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _repair_torn_tail(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())             # durable before the tick reports success


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break        # torn trailing write (crash before fsync); prefix stands
            raise            # a broken line MID-file is real corruption, not tolerated
    return out


class WriterLockError(RuntimeError):
    """A second writer tried to hold the same ledger's lock."""


@contextmanager
def writer_lock(lock_path: Path):
    """The 'never run two writers' rule as a MECHANISM, not a convention: an
    exclusive advisory lock, non-blocking, so a second tick on the same ledger
    raises (WriterLockError) instead of interleaving appends. Unix-only (fcntl);
    the loop runs on Linux/macOS."""
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise WriterLockError(
            f"another writer holds {lock_path.name}; refusing to run two "
            "writers on the same ledger") from exc
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# --- pure strategy/accounting primitives (unit-tested) ---

# Sanity rails on incoming prices — a feed-break detector, not a domain truth.
# Bounds are PER CURRENCY because the native /MWh price scale differs by market:
# EUR/GBP sit in the hundreds, USD (ERCOT) reaches the system offer cap ~$9000/MWh
# in scarcity, and JPY (JEPX, ¥/MWh) is naturally in the thousands-to-hundreds-of-
# thousands. A single EUR-scaled cap (the old -500..4000) silently REFUSED every
# JP fetch as "insane" (11250 ¥/MWh = 11.25 ¥/kWh is normal) — so JP never stored
# a price and accrued no track record from launch (incident 2026-09-02). The same
# cap would refuse a legitimate ERCOT scarcity print > $4000/MWh.
PRICE_BOUNDS: dict[str, tuple[float, float]] = {
    "EUR": (-500.0, 4000.0),      # EU SDAC technical bounds
    "GBP": (-500.0, 4000.0),      # GB (Elexon) — same order of magnitude
    "USD": (-500.0, 9000.0),      # ERCOT — offer cap historically up to $9000/MWh
    "JPY": (-1000.0, 400000.0),   # JEPX ¥/MWh — crises ~250 ¥/kWh = 250,000 ¥/MWh
}
DEFAULT_PRICE_BOUNDS = (-500.0, 4000.0)


def validate_prices(new: dict[str, float], currency: str = "EUR") -> str | None:
    """The append-only ledger's one unguarded failure mode is settling against
    a CORRUPTED feed (schema drift, unit change): unlike a missed day, that
    poisons the record permanently and invisibly. A fetch that fails these
    rails is treated exactly like a failed fetch — retried, then refused. Bounds
    are per the market's CURRENCY (PRICE_BOUNDS) so a market whose native /MWh
    scale is large (JPY) is not mistaken for a corrupt EUR feed."""
    lo, hi = PRICE_BOUNDS.get(currency, DEFAULT_PRICE_BOUNDS)
    for ts, p in new.items():
        if not isinstance(p, (int, float)) or isinstance(p, bool) \
                or not isfinite(p) or not (lo <= p <= hi):
            return (f"insane price {p!r} at {ts} ({currency} bounds {lo}..{hi}) "
                    f"— refusing to merge this fetch")
    return None


def day_profile(prices: dict[str, float], d: date) -> dict[int, float]:
    """{hour: price} for day d, whatever hours exist."""
    key = d.isoformat()
    return {int(ts[11:13]): p for ts, p in prices.items() if ts[:10] == key}


def pick_hours(profile: dict[int, float], n: int = N_HOURS) -> tuple[list[int], list[int]]:
    """(buy_hours, sell_hours): the n cheapest / n dearest hours of `profile`.
    Ties break toward the earlier hour (sorted by (price, hour)) — deterministic."""
    by_price = sorted(profile, key=lambda h: (profile[h], h))
    return sorted(by_price[:n]), sorted(by_price[-n:])


def kendall_tau(x: list[float], y: list[float]) -> float | None:
    """Tie-aware Kendall tau-b between two equal-length sequences.

    Recorded per settlement because rank correlation of the forecast — not
    level error — is what predicts battery-arbitrage value (research review
    2026-08: tau >= ~0.85 captures ~97-100% of perfect-foresight revenue).
    Returns None when undefined (fewer than 2 points, or either side fully
    tied — e.g. a constant price day)."""
    n = len(x)
    if n < 2 or len(y) != n:
        return None
    conc = disc = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            a = (x[i] > x[j]) - (x[i] < x[j])
            b = (y[i] > y[j]) - (y[i] < y[j])
            if a == 0:
                ties_x += 1
            if b == 0:
                ties_y += 1
            if a * b > 0:
                conc += 1
            elif a * b < 0:
                disc += 1
    n0 = n * (n - 1) // 2
    denom = ((n0 - ties_x) * (n0 - ties_y)) ** 0.5
    if denom == 0:
        return None
    return round((conc - disc) / denom, 3)


def pnl_eur(buy_hours: list[int], sell_hours: list[int],
            actual: dict[int, float]) -> float:
    """Realized P&L of the battery cycle against day-T actual prices.
    Buy POWER_MW in each buy hour; sell POWER_MW*RT_EFF in each sell hour;
    fees on every MWh moved in either direction."""
    cost = sum(actual[h] * POWER_MW for h in buy_hours)
    revenue = sum(actual[h] * POWER_MW * RT_EFF for h in sell_hours)
    moved = POWER_MW * len(buy_hours) + POWER_MW * RT_EFF * len(sell_hours)
    return round(revenue - cost - FEE_EUR_MWH * moved, 2)


def persistence_basis(prices: dict[str, float], today: date):
    """v1 basis: today's (the last known day's) profile, complete or nothing."""
    basis = day_profile(prices, today)
    if len(basis) < MIN_BASIS_HOURS:
        return None, f"basis day {today} incomplete ({len(basis)} hours)"
    return basis, None


def weekly_basis(prices: dict[str, float], today: date):
    """Shadow baseline (pre-registered 2026-08-22): same hour LAST WEEK —
    p(d-7,h), the EPF literature's canonical naive reference (it carries
    weekly seasonality that daily persistence misses; rMAE normalizes
    against it). The panel's missing standard baseline, registered at
    freeze-lift per the gate backlog."""
    basis = day_profile(prices, today - timedelta(days=6))
    if len(basis) < MIN_BASIS_HOURS:
        return None, (f"basis day {today - timedelta(days=6)} "
                      f"incomplete ({len(basis)} hours)")
    return basis, None


def climatology_basis(prices: dict[str, float], today: date):
    """Shadow-baseline basis: per-hour mean over the complete days in the
    trailing CLIM_WINDOW ending today. An hour enters when at least half those
    days carry it (the DST-spring day lacks 02:00)."""
    days = [p for i in range(CLIM_WINDOW)
            if len(p := day_profile(prices, today - timedelta(days=i))) >= MIN_BASIS_HOURS]
    if len(days) < CLIM_MIN_DAYS:
        return None, (f"only {len(days)} complete days in trailing {CLIM_WINDOW} "
                      f"— need >= {CLIM_MIN_DAYS}")
    basis = {}
    for h in range(24):
        vals = [d[h] for d in days if h in d]
        if len(vals) * 2 >= len(days):
            basis[h] = statistics.fmean(vals)
    return basis, None


def rankblend_basis(prices: dict[str, float], today: date):
    """Shadow baseline (pre-registered 2026-08-10): the mean of persistence's
    and climatology's per-hour RANKS. Motivation: over the first shared week
    each leg won on different days — averaging ranks is the cheapest ensemble
    that could dominate both. The basis values are average ranks, not prices:
    pick_hours ranks them identically, and settlement's kendall_tau is
    rank-based, so both remain meaningful. Needs BOTH legs (honest miss
    otherwise)."""
    p_basis, p_why = persistence_basis(prices, today)
    if p_basis is None:
        return None, f"persistence leg: {p_why}"
    c_basis, c_why = climatology_basis(prices, today)
    if c_basis is None:
        return None, f"climatology leg: {c_why}"
    hours = sorted(set(p_basis) & set(c_basis))
    if len(hours) < MIN_BASIS_HOURS:
        return None, f"only {len(hours)} hours shared by both legs"

    def ranks(basis: dict[int, float]) -> dict[int, int]:
        order = sorted(hours, key=lambda h: (basis[h], h))
        return {h: i for i, h in enumerate(order)}

    rp, rc = ranks(p_basis), ranks(c_basis)
    return {h: (rp[h] + rc[h]) / 2 for h in hours}, None


# Pre-registered strategy panel: PRIMARY first (its receipt drives the
# heartbeat); every additional entry is a shadow strategy on parallel receipts.
STRATEGIES = [
    {"strategy": STRATEGY, "strategy_version": STRATEGY_VERSION,
     "basis_fn": persistence_basis},
    {"strategy": "battery-2h2h-climatology", "strategy_version": "1",
     "basis_fn": climatology_basis},
    {"strategy": "battery-2h2h-rankblend", "strategy_version": "1",
     "basis_fn": rankblend_basis},
    {"strategy": "battery-2h2h-weekly", "strategy_version": "1",
     "basis_fn": weekly_basis},
]


# --- the tick ---

def tick(*, market: Market | None = None, fetch=None, today: date | None = None,
         sleep=time.sleep, now_fn=None) -> dict:
    """One idempotent daily pass for ONE market: update prices -> settle due
    receipts -> commit tomorrow's receipt (leak guard + clock guard
    permitting). Returns a summary dict. `market` defaults to Spain.
    `fetch`/`today`/`sleep`/`now_fn` injectable for tests; an injected `today`
    without `now_fn` assumes mid-window (11:00 market-local)."""
    market = market or _default_market()
    fetch = fetch or market.fetch
    if now_fn is not None:
        now = now_fn()
    elif today is not None:
        now = datetime.combine(today, datetime.min.time(),
                               tzinfo=market.tz).replace(hour=11)
    else:
        now = market_now(market.tz)
    today = today or now.date()
    tomorrow = today + timedelta(days=1)
    summary: dict = {"market": market.slug, "date": today.isoformat(),
                     "target": tomorrow.isoformat(),
                     "settled": [], "committed": [], "skipped": []}

    prices = load_prices(market.prices_path)
    last_ts = max(prices) if prices else "2026-01-26T23"
    fetch_from = date.fromisoformat(last_ts[:10])
    retries = market.fetch_retries
    for attempt in range(1 + len(retries)):
        try:
            fetched = fetch(fetch_from, tomorrow)
            bad = validate_prices(fetched, market.currency)
            if bad:
                raise ValueError(bad)
            prices.update(fetched)
            save_prices(prices, market.prices_path)
            summary.pop("fetch_error", None)
            break
        except (TypeError, AttributeError, NameError, ImportError):
            # A bug in OUR OWN code — never how a network/feed fails. Crash
            # honestly instead of masquerading as a fetch failure (which would
            # silently degrade to stale data and hide the bug). Feed-shaped
            # failures (OSError, bad JSON/XML, KeyError, bad zip, a
            # validate_prices ValueError) fall through to the handler below.
            raise
        except Exception as exc:
            # Stale data: settlement just waits; commitment may still be
            # possible (and is still leak-safe: the guard checks OUR dataset,
            # and without a fetch no new knowledge entered it).
            summary["fetch_error"] = str(exc)
            print(f"[esios-paper:{market.slug}] fetch failed "
                  f"(attempt {attempt + 1}/{1 + len(retries)}): {exc}")
            if attempt < len(retries):
                print(f"[esios-paper:{market.slug}] retrying in {retries[attempt]}s")
                sleep(retries[attempt])
            else:
                print(f"[esios-paper:{market.slug}] fetch gave up; "
                      "continuing with stored data")

    # 1) settle every unsettled receipt whose target day is fully published.
    #    Keyed by (target, strategy, version): parallel shadow receipts for the
    #    same target settle independently and never mask each other.
    settled_keys = {(r["target"], r["strategy"], r["strategy_version"])
                    for r in _load_jsonl(market.ledger_path)}
    for rec in _load_jsonl(market.receipts_path):
        target = rec["target"]
        key = (target, rec["strategy"], rec["strategy_version"])
        if key in settled_keys:
            continue
        actual = day_profile(prices, date.fromisoformat(target))
        if len(actual) < MIN_BASIS_HOURS or not all(
                h in actual for h in rec["buy_hours"] + rec["sell_hours"]):
            continue   # not published yet (or DST removed an hour we chose)
        oracle_buy, oracle_sell = pick_hours(actual)
        entry = {
            "target": target,
            "strategy": rec["strategy"], "strategy_version": rec["strategy_version"],
            "buy_hours": rec["buy_hours"], "sell_hours": rec["sell_hours"],
            "buy_prices": [actual[h] for h in rec["buy_hours"]],
            "sell_prices": [actual[h] for h in rec["sell_hours"]],
            "pnl_eur": pnl_eur(rec["buy_hours"], rec["sell_hours"], actual),
            "oracle_pnl_eur": pnl_eur(oracle_buy, oracle_sell, actual),
            "settled_at": datetime.now(timezone.utc).isoformat(),
        }
        entry["capture"] = (round(entry["pnl_eur"] / entry["oracle_pnl_eur"], 3)
                            if entry["oracle_pnl_eur"] > 0 else None)
        # Regime telemetry (research review 2026-08: watch spread erosion and
        # negative-price saturation — the conditions that make persistence
        # look good are structural, not permanent). Gross market quantities,
        # no efficiency/fees: these describe the DAY, not the strategy.
        by_price = sorted(actual.values())
        entry["neg_hours"] = sum(1 for p in by_price if p < 0)
        entry["tb2_spread"] = round(
            sum(by_price[-N_HOURS:]) - sum(by_price[:N_HOURS]), 2)
        # Rank quality of the forecast the receipt actually ranked on (receipts
        # before 2026-08-09 carry no basis_profile -> tau stays None).
        entry["tau"] = None
        if "basis_profile" in rec:
            basis = {int(h): p for h, p in rec["basis_profile"].items()}
            hours = sorted(h for h in basis if h in actual)
            entry["tau"] = kendall_tau([basis[h] for h in hours],
                                       [actual[h] for h in hours])
        _append(market.ledger_path, entry)
        summary["settled"].append(entry)
        settled_keys.add(key)

    # 2) commit tomorrow's receipts — the leak guard first (per TARGET: once the
    #    day is published, NO strategy may commit), then one receipt per
    #    pre-registered strategy, each idempotent on (target, strategy).
    target = tomorrow
    if day_profile(prices, target):
        summary["skipped"].append(f"prices for {target} already published — "
                                  "commit window missed; no receipts (leak guard)")
    elif now.date() >= tomorrow or (
            now.date() == today and now.hour >= market.deadline_hour):
        summary["skipped"].append(
            f"past the {market.deadline_hour}:00 {market.tz.key} commit "
            f"deadline ({now:%H:%M}) — no receipts (clock guard); a missed "
            "day is honest, a post-publication receipt is worthless")
    else:
        existing = {(r["target"], r["strategy"])
                    for r in _load_jsonl(market.receipts_path)}
        for spec in STRATEGIES:
            name = spec["strategy"]
            if (target.isoformat(), name) in existing:
                summary["skipped"].append(
                    f"{name}: receipt for {target} already committed (idempotent)")
                continue
            basis, why = spec["basis_fn"](prices, today)
            if basis is None:
                summary["skipped"].append(f"{name}: {why} — cannot commit")
                continue
            buy, sell = pick_hours(basis)
            receipt = {
                "target": target.isoformat(), "basis_day": today.isoformat(),
                "buy_hours": buy, "sell_hours": sell,
                # The exact profile the strategy ranked on (climatology: the
                # trailing mean, NOT yesterday) — lets settlement score rank
                # quality (kendall_tau) of the forecast as committed.
                "basis_profile": {str(h): round(p, 4)
                                  for h, p in sorted(basis.items())},
                "strategy": name, "strategy_version": spec["strategy_version"],
                "params": {"power_mw": POWER_MW, "rt_eff": RT_EFF,
                           "fee_eur_mwh": FEE_EUR_MWH, "currency": market.currency},
                "committed_at": datetime.now(timezone.utc).isoformat(),
            }
            _append(market.receipts_path, receipt)
            summary["committed"].append(receipt)

    # The heartbeat's one question: does the PRIMARY receipt for tomorrow stand?
    summary["primary_receipt_stands"] = any(
        r["target"] == target.isoformat() and r["strategy"] == STRATEGY
        for r in _load_jsonl(market.receipts_path))
    return summary
