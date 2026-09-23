"""Tests for the paper loop. The receipts are only worth keeping if the leak
guard provably refuses oracle commits, the P&L math is exact, and the tick is
idempotent — each is pinned here with injected fetch/today (no network)."""
from __future__ import annotations

import json
from datetime import date

import pytest

import talea.loop as loop
from talea.loop import day_profile, pick_hours, pnl_eur, tick


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "PRICES", tmp_path / "prices.json")
    monkeypatch.setattr(loop, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(loop, "LEDGER", tmp_path / "ledger.jsonl")


def _day(d: str, prices: list[float]) -> dict[str, float]:
    return {f"{d}T{h:02d}": p for h, p in enumerate(prices)}


def _flat_day(d: str, base: float, cheap=(3, 4), dear=(20, 21)) -> dict[str, float]:
    prices = [base] * 24
    for h in cheap:
        prices[h] = base - 50
    for h in dear:
        prices[h] = base + 50
    return _day(d, prices)


# --- primitives ---

def test_pick_hours_cheapest_and_dearest_deterministic_ties():
    profile = {h: 10.0 for h in range(24)}
    profile[5] = 1.0
    profile[18] = 99.0
    buy, sell = pick_hours(profile)
    assert 5 in buy and 18 in sell
    assert buy == sorted(buy) and sell == sorted(sell)
    # all-tied profile: earliest hours win on both sides, deterministically
    b2, s2 = pick_hours({h: 7.0 for h in range(24)})
    assert b2 == [0, 1] and s2 == [22, 23]


def test_pnl_math_exact():
    actual = {3: 10.0, 4: 20.0, 20: 100.0, 21: 110.0}
    # cost = 10+20 = 30; revenue = 0.85*(100+110) = 178.5
    # fees = 0.5 * (2 + 1.7) = 1.85 -> pnl = 178.5 - 30 - 1.85 = 146.65
    assert pnl_eur([3, 4], [20, 21], actual) == pytest.approx(146.65)


def test_pnl_negative_prices_pay_you_to_buy():
    actual = {3: -5.0, 4: 0.0, 20: 60.0, 21: 60.0}
    # cost = -5; revenue = 0.85*120 = 102; fees 1.85 -> 105.15
    assert pnl_eur([3, 4], [20, 21], actual) == pytest.approx(105.15)


def test_market_today_is_madrid_not_machine_local():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # pins the market-day rule: Europe/Madrid, regardless of machine TZ
    assert loop.market_today() == datetime.now(ZoneInfo("Europe/Madrid")).date()
    assert str(loop.MARKET_TZ) == "Europe/Madrid"


def test_kendall_tau_perfect_reversed_ties_and_degenerate():
    assert loop.kendall_tau([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert loop.kendall_tau([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    # one tied pair in y: tau-b = 5 / sqrt(6*5)
    assert loop.kendall_tau([1, 2, 3, 4], [1, 2, 2, 4]) == pytest.approx(
        5 / (30 ** 0.5), abs=1e-3)
    assert loop.kendall_tau([7, 7, 7], [1, 2, 3]) is None    # fully tied side
    assert loop.kendall_tau([1], [1]) is None                # too short
    assert loop.kendall_tau([1, 2], [1]) is None             # length mismatch


# --- the tick ---

def test_commit_uses_yesterday_profile_and_writes_receipt():
    prices = _flat_day("2026-08-01", 60, cheap=(2, 14), dear=(9, 22))
    tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    # one complete day: persistence commits; climatology honestly skips (no history)
    rec = json.loads(loop.RECEIPTS.read_text().strip())
    assert rec["target"] == "2026-08-02" and rec["basis_day"] == "2026-08-01"
    assert rec["buy_hours"] == [2, 14] and rec["sell_hours"] == [9, 22]
    assert rec["strategy"] == "battery-2h2h-persistence"


def test_leak_guard_refuses_commit_when_target_published():
    prices = {**_flat_day("2026-08-01", 60), **_flat_day("2026-08-02", 70)}
    s = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    assert s["committed"] == []                # NO strategy may commit
    assert any("leak guard" in m for m in s["skipped"])
    assert not loop.RECEIPTS.exists()          # no receipt was written at all


def test_tick_idempotent_one_receipt_per_target():
    prices = _flat_day("2026-08-01", 60)
    tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    s2 = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    assert any("already committed" in m for m in s2["skipped"])
    assert len(loop.RECEIPTS.read_text().strip().splitlines()) == 1


def test_incomplete_basis_day_refuses_commit():
    prices = {f"2026-08-01T{h:02d}": 50.0 for h in range(12)}   # half a day
    s = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    assert s["committed"] == [] and any("incomplete" in m for m in s["skipped"])


def test_settlement_joins_receipt_to_truth_and_is_final():
    d1 = _flat_day("2026-08-01", 60, cheap=(3, 4), dear=(20, 21))
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))
    # next day: target published; strategy hours realize the same shape
    d2 = _flat_day("2026-08-02", 80, cheap=(3, 4), dear=(20, 21))
    s = tick(fetch=lambda a, b: {**d1, **d2}, today=date(2026, 8, 2))
    assert len(s["settled"]) == 1
    e = s["settled"][0]
    assert e["target"] == "2026-08-02"
    # buy 30+30=60; sell 0.85*(130+130)=221; fees 1.85 -> 159.15; oracle equal
    assert e["pnl_eur"] == pytest.approx(159.15)
    assert e["capture"] == pytest.approx(1.0)
    # settling again must not double-append
    s3 = tick(fetch=lambda a, b: {**d1, **d2}, today=date(2026, 8, 2))
    assert s3["settled"] == []
    assert len(loop.LEDGER.read_text().strip().splitlines()) == 1


def test_receipt_stores_basis_profile_and_settlement_scores_tau():
    d1 = _flat_day("2026-08-01", 60)
    s1 = tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))
    rec = s1["committed"][0]
    # the committed profile is exactly what the strategy ranked on
    assert rec["basis_profile"]["3"] == 10.0
    assert len(rec["basis_profile"]) == 24
    # actual day has the same shape -> identical ranking -> tau 1.0
    d2 = _flat_day("2026-08-02", 90)
    s2 = tick(fetch=lambda a, b: {**d1, **d2}, today=date(2026, 8, 2))
    e = s2["settled"][0]
    assert e["tau"] == 1.0
    # a shape break must show up as tau < 1 (cheap/dear hours moved)
    d3 = _flat_day("2026-08-03", 90, cheap=(10, 11), dear=(1, 2))
    s3 = tick(fetch=lambda a, b: {**d1, **d2, **d3}, today=date(2026, 8, 3))
    e3 = s3["settled"][0]
    assert e3["tau"] is not None and e3["tau"] < 1.0


def test_settlement_records_regime_fields():
    d1 = _flat_day("2026-08-01", 60)                      # cheap 10, dear 110
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))
    prices2 = [80.0] * 24
    prices2[3], prices2[4] = -5.0, -1.0                   # 2 negative hours
    prices2[20], prices2[21] = 195.0, 205.0
    d2 = _day("2026-08-02", prices2)
    s = tick(fetch=lambda a, b: {**d1, **d2}, today=date(2026, 8, 2))
    e = s["settled"][0]
    assert e["neg_hours"] == 2
    # gross TB2: (205+195) - (-5 + -1) = 406, no efficiency/fees applied
    assert e["tb2_spread"] == pytest.approx(406.0)


def test_legacy_receipt_without_basis_profile_settles_with_tau_none():
    d1 = _flat_day("2026-08-01", 60)
    d2 = _flat_day("2026-08-02", 80)
    loop.save_prices({**d1, **d2})
    loop._append(loop.RECEIPTS, {
        "target": "2026-08-02", "basis_day": "2026-08-01",
        "buy_hours": [3, 4], "sell_hours": [20, 21],
        "strategy": loop.STRATEGY, "strategy_version": "1",
        "params": {}, "committed_at": "2026-08-01T09:00:00+00:00"})
    s = tick(fetch=lambda a, b: {**d1, **d2}, today=date(2026, 8, 2))
    e = s["settled"][0]
    assert e["tau"] is None
    assert e["pnl_eur"] > 0     # settlement itself unaffected


def test_second_market_runs_in_isolation(tmp_path):
    """Multi-market foundation: a non-ES market commits/settles into its OWN
    Data/<slug>/ tree, with its own timezone/deadline/currency, and never
    touches Spain's files (the autouse fixture's ES paths)."""
    from datetime import datetime
    de = loop.Market.make("de", "Europe/Berlin", fetch=None,
                          deadline_hour=12, currency="EUR", root=tmp_path)
    d1 = _flat_day("2026-08-01", 60, cheap=(3, 4), dear=(20, 21))
    s1 = tick(market=de, fetch=lambda a, b: d1, today=date(2026, 8, 1))
    assert s1["market"] == "de"
    assert (tmp_path / "de" / "receipts.jsonl").exists()
    assert not loop.RECEIPTS.exists()            # ES tree untouched
    rec = json.loads((tmp_path / "de" / "receipts.jsonl").read_text().splitlines()[0])
    assert rec["params"]["currency"] == "EUR"
    # DE deadline is 12:00 Berlin — a 12:30 tick must refuse (clock guard)
    late = lambda: datetime(2026, 8, 2, 12, 30, tzinfo=de.tz)
    s2 = tick(market=de, fetch=lambda a, b: d1, today=date(2026, 8, 2),
              sleep=lambda _s: None, now_fn=late)
    assert any("clock guard" in m for m in s2["skipped"])
    # settle DE's 08-02 receipt independently
    d2 = _flat_day("2026-08-02", 80, cheap=(3, 4), dear=(20, 21))
    s3 = tick(market=de, fetch=lambda a, b: {**d1, **d2}, today=date(2026, 8, 2))
    assert len(s3["settled"]) >= 1
    assert (tmp_path / "de" / "ledger.jsonl").exists()


def test_clock_guard_refuses_commit_after_deadline_even_with_stale_dataset():
    """The 2026-08-20 review finding: all fetches fail, tick runs late — the
    dataset-relative leak guard can't see published prices, so only the
    clock refuses. A post-publication receipt must be impossible."""
    from datetime import datetime
    d1 = _flat_day("2026-08-01", 60)
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))

    def boom(a, b):
        raise OSError("network down all day")
    late = lambda: datetime(2026, 8, 1, 17, 0, tzinfo=loop.MARKET_TZ)
    s = tick(fetch=boom, today=date(2026, 8, 1), sleep=lambda _s: None,
             now_fn=late)
    assert s["committed"] == []
    assert any("clock guard" in m for m in s["skipped"])
    # before the deadline, the same stale state commits fine
    early = lambda: datetime(2026, 8, 1, 11, 5, tzinfo=loop.MARKET_TZ)
    s2 = tick(fetch=boom, today=date(2026, 8, 1), sleep=lambda _s: None,
              now_fn=early)
    assert s2["skipped"] == [] or not any("clock guard" in m
                                          for m in s2["skipped"])


def test_weekly_baseline_uses_same_weekday_last_week():
    from datetime import timedelta
    prices = {}
    # day target-7 (2026-07-26) cheap at (2,3), dear at (18,19); yesterday
    # (2026-08-01) cheap at (10,11), dear at (20,21)
    prices.update(_flat_day("2026-07-26", 50, cheap=(2, 3), dear=(18, 19)))
    for i in range(6):
        d = (date(2026, 7, 27) + timedelta(days=i)).isoformat()
        prices.update(_flat_day(d, 60, cheap=(10, 11), dear=(20, 21)))
    s = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    weekly = [r for r in s["committed"]
              if r["strategy"] == "battery-2h2h-weekly"]
    assert weekly and weekly[0]["buy_hours"] == [2, 3]
    assert weekly[0]["sell_hours"] == [18, 19]     # last week's shape, not yesterday's


def test_fetch_failure_keeps_loop_alive_and_leak_safe():
    d1 = _flat_day("2026-08-01", 60)
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))

    def boom(a, b):
        raise OSError("network down")
    s = tick(fetch=boom, today=date(2026, 8, 2), sleep=lambda _s: None)
    assert "fetch_error" in s
    # basis day 2026-08-02 has no data -> cannot commit; honest skip, no crash
    assert s["committed"] == []


def test_our_code_bug_crashes_not_swallowed_as_fetch_failure():
    # A programming error in the fetch path (TypeError/AttributeError/...) must
    # propagate and crash the tick — never be logged as a fetch_error and
    # silently degrade to stale data, which would hide the bug.
    d1 = _flat_day("2026-08-01", 60)
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))

    def buggy(a, b):
        raise TypeError("'NoneType' object is not subscriptable")   # our bug
    with pytest.raises(TypeError):
        tick(fetch=buggy, today=date(2026, 8, 2), sleep=lambda _s: None)


def test_fetch_start_reaches_back_to_an_interior_hole():
    """Audit finding F1 (2026-09-23): DE's dataset held 09-12 and 09-14 but no
    09-13 hour at all, and the tick's fetch window started at the NEWEST stored
    day, so the hole was never re-requested and four 09-13 receipts sat
    unsettled for 10 days. The window must start at the earliest missing day
    (bounded by the lookback), not at the newest stored one."""
    prices = {**_flat_day("2026-09-12", 60), **_flat_day("2026-09-14", 60)}
    assert loop.fetch_start(prices, date(2026, 9, 15)) == date(2026, 9, 13)
    # no hole -> unchanged behaviour: start at the newest stored day
    full = {**prices, **_flat_day("2026-09-13", 60)}
    assert loop.fetch_start(full, date(2026, 9, 15)) == date(2026, 9, 14)
    # a hole older than the lookback is history, not chased every tick
    assert loop.fetch_start(prices, date(2026, 10, 30)) == date(2026, 9, 14)
    # empty dataset -> the fixed origin, as before
    assert loop.fetch_start({}, date(2026, 9, 15)) == date(2026, 1, 26)


def test_merge_fetched_fills_a_hole_but_never_rewrites_settled_history():
    stored = {**_flat_day("2026-09-12", 60), **_flat_day("2026-09-14", 60)}
    revised = {k: v + 1 for k, v in _flat_day("2026-09-12", 60).items()}
    fetched = {**revised, **_flat_day("2026-09-13", 70),
               **_flat_day("2026-09-14", 80), "2026-09-15T00": 5.0}
    out = loop.merge_fetched(stored, fetched)
    assert out["2026-09-12T00"] == 60.0          # earlier day: a revision is ignored
    assert out["2026-09-13T00"] == 70.0          # the hole is filled
    assert out["2026-09-14T00"] == 80.0          # newest stored day may complete/refresh
    assert out["2026-09-15T00"] == 5.0
    assert stored["2026-09-12T00"] == 60.0       # pure: input untouched


def test_tick_backfills_a_skipped_day_and_settles_its_receipts():
    """End-to-end reproduction of DE 2026-09-13: the day the feed skipped must
    be re-requested on a later tick (the fake feed honours [start, end], so a
    window starting at the newest stored day can never return it) and the
    receipts committed for it must then settle."""
    feed: dict[str, float] = {}

    calls: list[tuple[date, date]] = []

    def fetch(a, b):
        calls.append((a, b))
        return {k: v for k, v in feed.items() if a.isoformat() <= k[:10] <= b.isoformat()}

    feed.update(_flat_day("2026-09-12", 60))
    s = tick(fetch=fetch, today=date(2026, 9, 12))
    assert any(r["target"] == "2026-09-13" for r in s["committed"])
    # 09-13 tick: the feed serves 09-14 but SKIPS 09-13 (the SMARD gap)
    feed.update(_flat_day("2026-09-14", 60))
    s = tick(fetch=fetch, today=date(2026, 9, 13))
    assert s["settled"] == []
    # 09-14 tick: the feed now has 09-13 too; the window must reach back for it
    feed.update(_flat_day("2026-09-13", 60, cheap=(1, 2), dear=(18, 19)))
    s = tick(fetch=fetch, today=date(2026, 9, 14))
    assert calls[-1][0] == date(2026, 9, 13), calls[-1]
    assert {e["target"] for e in s["settled"]} == {"2026-09-13"}
    stored = loop.load_prices()
    assert "2026-09-13T01" in stored and stored["2026-09-13T01"] == 10.0


def test_validate_prices_rails():
    ok = {"2026-08-01T03": -5.0, "2026-08-01T04": 3999.9}
    assert loop.validate_prices(ok) is None
    assert "insane" in loop.validate_prices({"t": float("nan")})
    assert "insane" in loop.validate_prices({"t": 9999.0})
    assert "insane" in loop.validate_prices({"t": -600.0})
    assert "insane" in loop.validate_prices({"t": "82.0"})
    assert "insane" in loop.validate_prices({"t": True})


def test_validate_prices_bounds_are_currency_aware():
    """Incident 2026-09-02: the sanity rail was a single EUR-scaled cap
    (-500..4000) applied to every market, so it REFUSED every JP fetch — JEPX
    ¥/MWh is naturally in the thousands (11250 ¥/MWh = 11.25 ¥/kWh is normal) —
    and JP never stored a price. Bounds must be per-currency."""
    jp = {"2026-01-26T00": 11250.0, "2026-01-27T18": 250000.0}  # incl. a winter-crisis print
    assert loop.validate_prices(jp, "JPY") is None              # normal JP now passes
    assert "insane" in loop.validate_prices(jp, "EUR")          # the exact old bug
    # ERCOT scarcity: USD/MWh can reach the offer cap ~$9000 — legitimate, must pass
    assert loop.validate_prices({"t": 8000.0}, "USD") is None
    assert "insane" in loop.validate_prices({"t": 8000.0}, "EUR")
    # a genuinely corrupt JP feed is still refused (far above the JPY cap)
    assert "insane" in loop.validate_prices({"t": 5_000_000.0}, "JPY")
    # unknown currency falls back to the conservative EUR bounds
    assert "insane" in loop.validate_prices({"t": 9999.0}, "XYZ")


def test_silent_market_fetch_fails_fast_no_retry_backoff(tmp_path):
    """A silent market (fetch_retries=()) must NOT inherit ES's 20-min backoff:
    it runs before ES in server_tick.sh, so a stalled silent fetch would delay
    the primary tick. One attempt, zero sleeps."""
    naps, attempts = [], []

    def boom(a, b):
        attempts.append(1)
        raise OSError("down")
    m = loop.Market.make("de", "Europe/Berlin", boom, deadline_hour=12,
                         root=tmp_path)   # make() defaults fetch_retries=()
    s = tick(market=m, today=date(2026, 8, 1), sleep=naps.append)
    assert len(attempts) == 1 and naps == []      # no retries, no sleeps
    assert "fetch_error" in s


def test_corrupt_feed_is_refused_and_never_touches_the_dataset():
    d1 = _flat_day("2026-08-01", 60)
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))
    before = loop.PRICES.read_text()
    poisoned = {**d1, **_day("2026-08-02", [80.0] * 23 + [999999.0])}
    s = tick(fetch=lambda a, b: poisoned, today=date(2026, 8, 2),
             sleep=lambda _s: None)
    assert "insane" in s["fetch_error"]
    assert loop.PRICES.read_text() == before      # dataset of record untouched
    assert s["settled"] == []                     # nothing settled on bad data


def test_fetch_retries_with_backoff_then_gives_up():
    calls, naps = [], []

    def boom(a, b):
        calls.append(1)
        raise OSError("network down")
    s = tick(fetch=boom, today=date(2026, 8, 2), sleep=naps.append)
    # one initial attempt plus one retry per configured delay, spaced as configured
    assert len(calls) == 1 + len(loop.FETCH_RETRY_DELAYS_S)
    assert naps == list(loop.FETCH_RETRY_DELAYS_S)
    assert "fetch_error" in s


def test_fetch_retry_recovers_transient_failure_and_commits():
    d1 = _flat_day("2026-08-01", 60)
    tick(fetch=lambda a, b: d1, today=date(2026, 8, 1))

    attempts, naps = [], []

    def flaky(a, b):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transient DNS failure")
        return _flat_day("2026-08-02", 70)
    s = tick(fetch=flaky, today=date(2026, 8, 2), sleep=naps.append)
    # second attempt succeeded: no error reported, fresh basis, receipt stands
    assert "fetch_error" not in s
    assert naps == [loop.FETCH_RETRY_DELAYS_S[0]]
    assert s["settled"] and s["settled"][0]["target"] == "2026-08-02"
    assert any(r["target"] == "2026-08-03" for r in s["committed"])
    assert s["primary_receipt_stands"]


# --- CLI market routing (silent markets skip ES-only side effects) ---

def test_cmd_tick_silent_market_skips_heartbeat_email_git_ots(monkeypatch):
    import talea.__main__ as cli
    import talea.markets as markets
    calls = []
    monkeypatch.setattr(cli, "tick", lambda **kw: {
        "market": kw["market"].slug, "date": "2026-08-22", "target": "2026-08-23",
        "settled": [], "committed": [], "skipped": []})
    for name in ("ots_stamp", "git_backup", "heartbeat", "email_digest"):
        monkeypatch.setattr(cli, name, lambda *a, n=name, **k: calls.append(n))
    rc = cli.cmd_tick("de")
    assert rc == 0
    assert calls == ["ots_stamp"]            # silent markets OTS-anchor, but skip heartbeat/email/git
    # ES path still fires them
    monkeypatch.setattr(cli, "tick", lambda **kw: {
        "date": "2026-08-22", "target": "2026-08-23", "settled": [],
        "committed": [], "skipped": [], "primary_receipt_stands": True})
    cli.cmd_tick()
    assert set(calls) == {"ots_stamp", "git_backup", "heartbeat", "email_digest"}


def test_cmd_tick_unknown_market_errors(monkeypatch):
    import talea.__main__ as cli
    assert cli.cmd_tick("atlantis") == 2


# --- OpenTimestamps attestation (best-effort, never fatal) ---

def test_ots_manifest_binds_current_audit_trail(monkeypatch, tmp_path):
    import talea.__main__ as cli
    monkeypatch.setattr(cli, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "ledger.jsonl")
    (tmp_path / "receipts.jsonl").write_text('{"target": "2026-08-09"}\n')
    m1 = cli.ots_manifest("2026-08-09")
    assert "sha256(receipts.jsonl)=" in m1
    assert "sha256(ledger.jsonl)=absent" in m1
    # manifest must change when the audit trail changes
    (tmp_path / "receipts.jsonl").write_text('{"target": "2026-08-10"}\n')
    assert cli.ots_manifest("2026-08-09") != m1


def test_ots_stamped_manifest_is_immutable_second_tick_gets_new_slot(
        monkeypatch, tmp_path):
    """Regression for the 2026-08-21 audit finding: the second tick of a day
    (audit trail changed by settlement) must NEVER rewrite an already-stamped
    manifest — it stamps a suffixed one. 12/13 proofs were invalidated by the
    old overwrite-then-skip behavior."""
    import talea.__main__ as cli
    monkeypatch.setattr(cli, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli, "OTS_DIR", tmp_path / "ots")

    def fake_stamp(cmd, **kw):
        Path(cmd[-1] + ".ots").write_bytes(b"proof")
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    from pathlib import Path
    (tmp_path / "receipts.jsonl").write_text("morning\n")
    cli.ots_stamp("2026-08-21", _run=fake_stamp)
    first = (tmp_path / "ots" / "2026-08-21.txt").read_text()
    # settlement changes the audit trail; tick runs again
    (tmp_path / "receipts.jsonl").write_text("morning\nevening\n")
    cli.ots_stamp("2026-08-21", _run=fake_stamp)
    assert (tmp_path / "ots" / "2026-08-21.txt").read_text() == first  # UNTOUCHED
    assert (tmp_path / "ots" / "2026-08-21-2.txt").exists()            # new slot
    assert (tmp_path / "ots" / "2026-08-21-2.txt.ots").exists()
    # third run, unchanged state: idempotent, no third slot
    cli.ots_stamp("2026-08-21", _run=fake_stamp)
    assert not (tmp_path / "ots" / "2026-08-21-3.txt").exists()


def test_ots_per_market_stamps_own_dir(monkeypatch, tmp_path):
    """DE OTS: silent markets anchor into their own Data/<slug>/ots with their
    own receipts/ledger, not the ES globals."""
    from pathlib import Path
    import talea.__main__ as cli
    (tmp_path / "de").mkdir()
    (tmp_path / "de" / "receipts.jsonl").write_text('{"t": "de"}\n')
    (tmp_path / "de" / "ledger.jsonl").write_text('{"t": "de"}\n')
    def fake_stamp(cmd, **kw):
        Path(cmd[-1] + ".ots").write_bytes(b"proof")
        class R: returncode = 0; stderr = ""; stdout = ""
        return R()
    cli.ots_stamp("2026-08-27", ots_dir=tmp_path / "de" / "ots",
                  receipts=tmp_path / "de" / "receipts.jsonl",
                  ledger=tmp_path / "de" / "ledger.jsonl", _run=fake_stamp)
    assert (tmp_path / "de" / "ots" / "2026-08-27.txt.ots").exists()
    man = (tmp_path / "de" / "ots" / "2026-08-27.txt").read_text()
    assert "sha256(receipts.jsonl)=" in man and "absent" not in man


def test_ots_stamp_failure_never_raises(monkeypatch, tmp_path):
    import talea.__main__ as cli
    monkeypatch.setattr(cli, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli, "OTS_DIR", tmp_path / "ots")

    def boom(*a, **k):
        raise OSError("no network")
    cli.ots_stamp("2026-08-09", _run=boom)          # must not raise
    class R:
        returncode = 1
        stderr = "calendar unreachable"
        stdout = ""
    cli.ots_stamp("2026-08-09", _run=lambda *a, **k: R())   # must not raise
    assert (tmp_path / "ots" / "2026-08-09.txt").exists()   # manifest still written


# --- email digest (best-effort inbox delivery) ---

def _cli_with_data(monkeypatch, tmp_path):
    import talea.__main__ as cli
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    rows = [
        {"target": "2026-08-13", "strategy": loop.STRATEGY,
         "pnl_eur": 322.13, "oracle_pnl_eur": 325.78, "capture": 0.989,
         "tau": 0.899, "neg_hours": 0, "tb2_spread": 396.26},
        {"target": "2026-08-13", "strategy": "battery-2h2h-climatology",
         "pnl_eur": 315.79, "oracle_pnl_eur": 325.78, "capture": 0.969,
         "tau": 0.855, "neg_hours": 0, "tb2_spread": 396.26},
    ]
    (tmp_path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    return cli


def test_digest_builds_settlement_race_and_bar(monkeypatch, tmp_path):
    cli = _cli_with_data(monkeypatch, tmp_path)
    subject, body = cli.build_digest()
    assert "esios digest 2026-08-13" in subject and "+322" in subject
    assert "+322.13" in body and "tau 0.899" in body
    # the digest is DESCRIPTIVE — it defers the verdict to the Option C bar and
    # never auto-declares "BAR MET" (referee-gated, superseded-method guard)
    assert "verdict via the Option C bar" in body and "BAR MET" not in body
    assert "ALERT" not in subject         # winning day, no alerts


def test_digest_never_auto_declares_bar_met(monkeypatch, tmp_path):
    # 30 days where climatology beats persistence — the OLD digest method would
    # have printed "BAR MET". The digest must NOT auto-declare it (a bar-met
    # claim is the referee-gated Option C verdict, not the digest's to make).
    import talea.__main__ as cli
    monkeypatch.setattr(cli, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(cli, "RECEIPTS", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    rows = []
    for i in range(30):
        d = f"2026-08-{i + 1:02d}"
        rows += [{"target": d, "strategy": loop.STRATEGY, "pnl_eur": 0.0,
                  "oracle_pnl_eur": 100.0, "capture": 0.0},
                 {"target": d, "strategy": "battery-2h2h-climatology",
                  "pnl_eur": 10.0, "oracle_pnl_eur": 100.0, "capture": 0.1}]
    (tmp_path / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    _, body = cli.build_digest()
    assert "BAR MET" not in body
    assert "verdict via the Option C bar" in body


def test_digest_includes_silent_markets(monkeypatch, tmp_path):
    cli = _cli_with_data(monkeypatch, tmp_path)
    (tmp_path / "de").mkdir()
    (tmp_path / "de" / "ledger.jsonl").write_text(json.dumps({
        "target": "2026-08-27", "strategy": loop.STRATEGY,
        "pnl_eur": 301.40, "oracle_pnl_eur": 301.40, "capture": 1.0}) + "\n")
    subject, body = cli.build_digest()
    assert "[DE public]" in body and "+301.40" in body
    # IT absent (no ledger) -> no IT section
    assert "[IT silent]" not in body


def test_digest_flags_losing_day(monkeypatch, tmp_path):
    cli = _cli_with_data(monkeypatch, tmp_path)
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    rows[0]["pnl_eur"] = -12.5
    (tmp_path / "ledger.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    subject, body = cli.build_digest()
    assert subject.startswith("ALERT — ")
    assert "losing day" in body


def test_email_digest_sends_on_settle_and_never_raises(monkeypatch, tmp_path):
    cli = _cli_with_data(monkeypatch, tmp_path)
    monkeypatch.setenv("ALERT_EMAIL_TO", "x@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "pw")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent.append(("tls", None))
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(("msg", m["Subject"]))
    cli.email_digest({"settled": [{"x": 1}], "primary_receipt_stands": True},
                     _smtp=FakeSMTP)
    assert ("login", "x@example.com") in sent
    assert any(k == "msg" for k, _ in sent)
    # commit-only pass: silent
    sent.clear()
    cli.email_digest({"settled": [], "primary_receipt_stands": True},
                     _smtp=FakeSMTP)
    assert sent == []
    # SMTP failure must never raise

    def boom(*a, **k):
        raise OSError("smtp down")
    cli.email_digest({"settled": [{"x": 1}], "primary_receipt_stands": True},
                     _smtp=boom)


def test_email_digest_alerts_on_trouble_even_without_settlement(monkeypatch, tmp_path):
    cli = _cli_with_data(monkeypatch, tmp_path)
    monkeypatch.setenv("ALERT_EMAIL_TO", "x@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "pw")
    subjects = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, m): subjects.append(m["Subject"])
    cli.email_digest({"settled": [], "primary_receipt_stands": False},
                     _smtp=FakeSMTP)
    assert subjects and subjects[0].startswith("ALERT — ")


# --- heartbeat semantics (success = today's receipt exists) ---

def test_heartbeat_signals_fail_when_no_receipt(monkeypatch):
    import talea.__main__ as cli
    calls = []
    monkeypatch.setenv("ESIOS_HEARTBEAT_URL", "https://hc.example/abc")
    cli.heartbeat(True, _urlopen=lambda u, timeout: calls.append(u))
    cli.heartbeat(False, _urlopen=lambda u, timeout: calls.append(u))
    assert calls == ["https://hc.example/abc", "https://hc.example/abc/fail"]


def test_heartbeat_silent_without_url(monkeypatch):
    import talea.__main__ as cli
    monkeypatch.delenv("ESIOS_HEARTBEAT_URL", raising=False)
    monkeypatch.setattr(cli, "REPO", cli.REPO / "nonexistent")   # no .env either
    cli.heartbeat(True, _urlopen=lambda u, timeout: (_ for _ in ()).throw(AssertionError))


def test_heartbeat_network_failure_never_raises(monkeypatch):
    import talea.__main__ as cli
    monkeypatch.setenv("ESIOS_HEARTBEAT_URL", "https://hc.example/abc")
    def boom(u, timeout):
        raise OSError("down")
    cli.heartbeat(True, _urlopen=boom)     # must not raise


# --- the climatology shadow baseline (parallel pre-registered receipts) ---

def _month(end: str, base_shape) -> dict[str, float]:
    """28 complete days ending at `end` (inclusive), each day from base_shape(i)."""
    from datetime import timedelta
    prices: dict[str, float] = {}
    end_d = date.fromisoformat(end)
    for i in range(28):
        d = (end_d - timedelta(days=27 - i)).isoformat()
        prices.update(_day(d, base_shape(i)))
    return prices


def test_climatology_commits_alongside_persistence_with_history():
    # every day: cheap at (3,4), dear at (20,21), varying level -> the hourly
    # mean has the same shape, so climatology picks the same windows.
    prices = _month("2026-08-01", lambda i: [
        50.0 + i + (-40 if h in (3, 4) else 40 if h in (20, 21) else 0)
        for h in range(24)])
    s = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    assert [r["strategy"] for r in s["committed"]] == [
        "battery-2h2h-persistence", "battery-2h2h-climatology",
        "battery-2h2h-rankblend", "battery-2h2h-weekly"]
    clim = s["committed"][1]
    assert clim["buy_hours"] == [3, 4] and clim["sell_hours"] == [20, 21]
    assert s["primary_receipt_stands"] is True


def test_parallel_receipts_settle_independently_and_idempotently():
    prices = _month("2026-08-01", lambda i: [
        50.0 + (-40 if h in (3, 4) else 40 if h in (20, 21) else 0)
        for h in range(24)])
    tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))      # 4 receipts
    truth = {**prices, **_flat_day("2026-08-02", 80, cheap=(3, 4), dear=(20, 21))}
    s = tick(fetch=lambda a, b: truth, today=date(2026, 8, 2))
    assert len(s["settled"]) == 4                                 # one per strategy
    assert {e["strategy"] for e in s["settled"]} == {
        "battery-2h2h-persistence", "battery-2h2h-climatology",
        "battery-2h2h-rankblend", "battery-2h2h-weekly"}
    # all picked the true windows here -> capture 1.0 across the panel
    assert all(e["capture"] == pytest.approx(1.0) for e in s["settled"])
    s3 = tick(fetch=lambda a, b: truth, today=date(2026, 8, 2))
    assert s3["settled"] == []                                    # never re-settled
    assert len(loop.LEDGER.read_text().strip().splitlines()) == 4


def test_climatology_averages_across_days_not_just_yesterday():
    # 27 days dear at (20,21); the LAST day dear at (9,10): persistence follows
    # yesterday's outlier, climatology follows the month's mean.
    def shape(i):
        dear = (9, 10) if i == 27 else (20, 21)
        return [50.0 + (-40 if h in (3, 4) else 40 if h in dear else 0)
                for h in range(24)]
    prices = _month("2026-08-01", shape)
    s = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    pers, clim, blend = s["committed"][:3]
    assert pers["sell_hours"] == [9, 10]                          # yesterday's shape
    assert clim["sell_hours"] == [20, 21]                         # the month's shape
    # the blend averages the two legs' ranks: buys where both agree, sells a
    # deterministic mix drawn from the union of the legs' sell windows
    assert blend["strategy"] == "battery-2h2h-rankblend"
    assert blend["buy_hours"] == [3, 4]
    assert set(blend["sell_hours"]) <= {9, 10, 20, 21}
    assert blend["sell_hours"] != pers["sell_hours"] or \
           blend["sell_hours"] != clim["sell_hours"]


def test_climatology_skips_without_enough_history_persistence_unaffected():
    prices = _flat_day("2026-08-01", 60)
    s = tick(fetch=lambda a, b: prices, today=date(2026, 8, 1))
    assert [r["strategy"] for r in s["committed"]] == ["battery-2h2h-persistence"]
    assert any("battery-2h2h-climatology" in m and "complete days" in m
               for m in s["skipped"])
    # the blend needs BOTH legs: it must skip too, naming the missing leg
    assert any("battery-2h2h-rankblend" in m and "climatology leg" in m
               for m in s["skipped"])
