"""The strategy comparison decides R1 promotion and R7 retirement, so every
boundary the rules draw is pinned here: the binomial p_iid, the calibration
floor (p_eff = max(p_iid, p_boot)), and — since Amendment 1 (ratified
2026-11-10) — forward-only data, the per-market Holm family, the 0.05/12
per-look threshold, R7's preconditions, the single confirmatory terminal look,
and the rule that nothing but counts is shown outside a decision look.
Failure-mode-first: most cases assert a decision is NOT reached on inputs a
naive reading would pass."""
import hashlib
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "compare_strategies",
    Path(__file__).resolve().parents[1] / "scripts" / "compare_strategies.py")
cs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cs)

CLIM, PERS = "battery-2h2h-climatology", "battery-2h2h-persistence"
RANK, WEEK = "battery-2h2h-rankblend", "battery-2h2h-weekly"
BEFORE = "2026-11-10T16:00:00+00:00"      # settled before the amendment
AFTER = "2026-11-12T16:00:00+00:00"       # settled after it


def surv(n, overrides=None):
    a = [cs.sign_test_p(w, n) for w in range(n + 1)]   # iid baseline (harmless)
    for w, p in (overrides or {}).items():
        a[w] = p
    return a


def cal_valid(survivals=None):
    return {"valid": True,
            "survival": {str(n): s for n, s in (survivals or {}).items()}}


def day(i, start=date(2026, 12, 1)):
    return (start + timedelta(days=i)).isoformat()


def row(target, strategy, pnl, *, oracle=100.0, settled=AFTER, version="1"):
    r = {"target": target, "strategy": strategy, "strategy_version": version,
         "pnl_eur": pnl, "settled_at": settled}
    if oracle is not None:
        r["oracle_pnl_eur"] = oracle
    return r


def head_to_head(a, b, wins, losses, *, ties=0, settled=AFTER, day0=0,
                 start=date(2026, 12, 1), win_by=10.0):
    """`wins` days A beats B, then `losses` days B beats A, then `ties` ties."""
    out, i = [], day0
    for a_pnl, b_pnl, count in ((50.0 + win_by, 50.0, wins), (50.0, 50.0 + win_by, losses),
                                (50.0, 50.0, ties)):
        for _ in range(count):
            out += [row(day(i, start), a, a_pnl, settled=settled),
                    row(day(i, start), b, b_pnl, settled=settled)]
            i += 1
    return out


def comp(look, rule, a, b):
    return next(c for c in look["comparisons"]
                if (c["rule"], c["a"], c["b"]) == (rule, a, b))


# ---- the binomial core --------------------------------------------------------

def test_sign_test_p_known_values():
    assert cs.sign_test_p(1, 1) == 0.5
    assert cs.sign_test_p(0, 1) == 1.0
    assert cs.sign_test_p(2, 2) == 0.25
    assert cs.sign_test_p(30, 30) == 1 / 2 ** 30


# ---- p_eff = max(p_iid, p_boot): the floor ------------------------------------

def test_calibration_can_only_harden_the_bar():
    # 28/30 wins: p_iid is tiny; the calibration says p_boot=0.03 -> p_eff hardens
    cal = cal_valid({30: surv(30, {28: 0.03})})
    peff, kind = cs.p_effective(cal, wins=28, n=30)
    assert peff == 0.03 and kind == "p_boot"
    assert peff > cs.sign_test_p(28, 30)          # strictly harder than iid


def test_iid_floor_protects_when_calibration_says_too_easy():
    # p_boot understates (0.001) but p_iid at 20/30 ~ 0.049 -> floor wins
    cal = cal_valid({30: surv(30, {20: 0.001})})
    peff, kind = cs.p_effective(cal, wins=20, n=30)
    assert peff == pytest.approx(cs.sign_test_p(20, 30)) and kind == "p_iid-floor"


def test_beyond_the_table_the_plain_sign_test_applies():
    # The frozen table covers n = 30..90. Amendment 1 (clause 4 notes): beyond
    # it the plain sign test applies — never an error, never a made-up p_boot.
    peff, kind = cs.p_effective(cal_valid({90: surv(90)}), wins=60, n=91)
    assert peff == cs.sign_test_p(60, 91) and "no table row" in kind
    assert cs.p_effective(None, wins=28, n=45)[0] == cs.sign_test_p(28, 45)


def test_frozen_calibration_reproduces_the_referee_number():
    """The statistical referee's reading of 2026-11-10 (ES rankblend, 40 wins of
    58 non-tied): p_eff = 0.0028, from the frozen table. Pins the real file to
    the code path that reads it."""
    assert cs.calibration_problem() is None       # the repo file IS the frozen one
    peff, kind = cs.p_effective(cs.load_calibration(), wins=40, n=58)
    assert peff == pytest.approx(0.0028) and kind == "p_boot"


# ---- the frozen file (clause 8) ------------------------------------------------

def _use_calibration(monkeypatch, tmp_path, cal, *, register=True):
    path = tmp_path / "sign_bar.json"
    path.write_text(json.dumps(cal))
    monkeypatch.setattr(cs, "CALIBRATION", path)
    if register:
        monkeypatch.setattr(cs, "CALIBRATION_SHA256",
                            hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def test_calibration_problem_missing_changed_or_invalid(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "CALIBRATION", tmp_path / "absent.json")
    assert "missing" in cs.calibration_problem()
    assert cs.load_calibration() is None
    # a file that is not the frozen one: one changed digit is enough
    _use_calibration(monkeypatch, tmp_path, cal_valid(), register=False)
    assert "not the frozen file" in cs.calibration_problem()
    # the frozen file itself marked invalid: the fallback regime has no per-look
    # threshold under the amendment, so the tool must refuse, not improvise
    _use_calibration(monkeypatch, tmp_path, {"valid": False})
    assert "not valid" in cs.calibration_problem()
    _use_calibration(monkeypatch, tmp_path, cal_valid())
    assert cs.calibration_problem() is None


# ---- pairing: shared days, ties, capture --------------------------------------

def test_only_shared_days_paired_and_tie_boundary():
    ev = cs.pair([
        row("d1", CLIM, 1.00), row("d1", PERS, 0.99),   # +0.01 -> win (rule A)
        row("d2", CLIM, 1.00), row("d2", PERS, 1.00),   # 0.00 -> tie
        row("d3", CLIM, 1.00),                           # PERS absent -> excluded
        row("d4", CLIM, 1.004), row("d4", PERS, 1.00),  # rounds to 0.00 -> tie
    ], CLIM, PERS)
    assert (ev["shared"], ev["wins"], ev["ties"], ev["losses"], ev["n"]) == (3, 1, 2, 0, 1)
    assert (ev["first_day"], ev["last_day"]) == ("d1", "d4")


def test_pooled_capture_is_sum_of_pnl_over_sum_of_oracle():
    # Clause 7. NOT the mean of daily captures: day 2 is ten times larger.
    ev = cs.pair([row("d1", CLIM, 90.0, oracle=100.0), row("d1", PERS, 50.0, oracle=100.0),
                  row("d2", CLIM, 500.0, oracle=1000.0), row("d2", PERS, 900.0, oracle=1000.0)],
                 CLIM, PERS)
    assert ev["pooled_capture_a"] == pytest.approx(590 / 1100)
    assert ev["pooled_capture_b"] == pytest.approx(950 / 1100)
    none = cs.pair([row("d1", CLIM, 9.0, oracle=None), row("d1", PERS, 5.0, oracle=None)],
                   CLIM, PERS)
    assert none["pooled_capture_a"] is None and none["pooled_capture_b"] is None


def test_a_row_without_realised_pnl_or_of_another_version_is_not_paired():
    rows = [row("d1", CLIM, 9.0), {"target": "d1", "strategy": PERS},      # unsettled
            row("d2", CLIM, 9.0, version="2"), row("d2", PERS, 5.0)]       # v2 is not v1
    assert cs.pair(rows, CLIM, PERS)["shared"] == 0
    assert cs.pair(rows, CLIM + "@v2", PERS)["wins"] == 1
    assert cs.strategy_id({"strategy": CLIM}) == CLIM                       # no version = v1


# ---- clause 6: forward only ----------------------------------------------------

def test_a_day_counts_only_if_both_rows_were_settled_after_the_amendment():
    at = cs.AMENDMENT_1_AT.isoformat()
    rows = [row("d1", RANK, 9.0, settled=BEFORE), row("d1", PERS, 5.0, settled=BEFORE),
            row("d2", RANK, 9.0, settled=AFTER), row("d2", PERS, 5.0, settled=BEFORE),
            row("d3", RANK, 9.0, settled=at), row("d3", PERS, 5.0, settled=AFTER),  # AT, not after
            row("d4", RANK, 9.0, settled=AFTER), row("d4", PERS, 5.0, settled=AFTER),
            {**row("d5", RANK, 9.0), "settled_at": None}, row("d5", PERS, 5.0)]     # unknown time
    ev = cs.pair(rows, RANK, PERS, after=cs.AMENDMENT_1_AT)
    assert (ev["shared"], ev["wins"], ev["first_day"]) == (1, 1, "d4")
    assert cs.pair(rows, RANK, PERS)["shared"] == 5       # without the cut-off: all five
    # a naive timestamp is read as UTC
    assert cs.settled_at({"settled_at": "2026-11-12T16:00:00"}).tzinfo is timezone.utc


def test_the_2026_11_01_readings_trigger_nothing():
    """Clause 6. Rankblend's 40 wins in 58 non-tied days were all settled before
    the amendment: they are not in any family, whatever they show."""
    look = cs.market_look("es", head_to_head(RANK, PERS, 40, 18, settled=BEFORE),
                          cal_valid(), primary=PERS)
    r1 = comp(look, "R1", RANK, PERS)
    assert (r1["n"], r1["eligible"], r1["p_eff"], look["k"]) == (0, False, None, 0)
    assert look["r1"][RANK] == "not at its precondition (0 of 30 non-tied days)"


def test_as_of_reproduces_an_earlier_look():
    later = "2026-12-25T16:00:00+00:00"
    rows = head_to_head(RANK, PERS, 3, 0) + head_to_head(RANK, PERS, 0, 4, day0=10,
                                                         settled=later)
    cut = datetime(2026, 12, 20, tzinfo=timezone.utc)
    assert cs.pair(rows, RANK, PERS, after=cs.AMENDMENT_1_AT)["n"] == 7
    assert cs.pair(rows, RANK, PERS, after=cs.AMENDMENT_1_AT, as_of=cut)["n"] == 3


# ---- clause 4: the per-look threshold is 0.05/12 -------------------------------

def test_a_result_that_passed_the_old_bar_does_not_pass_the_per_look_threshold():
    # 22 of 30: p_iid = 0.0081 — under 0.05, the bar before the amendment, and
    # over 0.05/12 = 0.004167. With k = 1 it must NOT be significant.
    look = cs.market_look("de", head_to_head(RANK, PERS, 22, 8), cal_valid(), primary=PERS)
    r1 = comp(look, "R1", RANK, PERS)
    assert look["k"] == 1 and r1["threshold"] == pytest.approx(0.05 / 12)
    assert 0.05 / 12 < r1["p_eff"] < 0.05 and not r1["significant"]
    assert look["r1"][RANK] == "sign test not met"
    # 23 of 30: p_iid = 0.0026 <= 0.004167 -> significant, and capture is higher
    look = cs.market_look("de", head_to_head(RANK, PERS, 23, 7), cal_valid(), primary=PERS)
    assert comp(look, "R1", RANK, PERS)["significant"] and look["r1"][RANK] == "BAR MET"


def test_precondition_is_30_non_tied_days_not_30_days():
    # 29 non-tied + 20 ties = 49 shared days: still not at the precondition
    look = cs.market_look("de", head_to_head(RANK, PERS, 29, 0, ties=20), cal_valid(),
                          primary=PERS)
    assert not comp(look, "R1", RANK, PERS)["eligible"] and look["k"] == 0


# ---- clause 1: Holm inside the market's family ---------------------------------

def test_holm_tightens_with_the_size_of_the_markets_family():
    # Alone (k = 1), 23 of 30 is significant. With a second shadow at the same
    # count, k = 2: the smallest p must be <= (0.05/12)/2 = 0.00208, and
    # 0.0026 is not — the step-down stops at the first, so NEITHER passes.
    rows = head_to_head(RANK, PERS, 23, 7) + [
        r for r in head_to_head(WEEK, PERS, 23, 7) if r["strategy"] == WEEK]
    look = cs.market_look("de", rows, cal_valid(), primary=PERS)
    assert look["k"] == 2
    assert comp(look, "R1", RANK, PERS)["threshold"] == pytest.approx(0.05 / 12 / 2)
    assert not any(c["significant"] for c in look["comparisons"])


def test_the_family_is_per_market_not_project_wide():
    # The same two results, one in each market: each family has k = 1, so each
    # is judged at 0.05/12. (The referee's DISCREPANT of 2026-11-10 turned on
    # exactly this reading.)
    de = cs.market_look("de", head_to_head(RANK, PERS, 23, 7), cal_valid(), primary=PERS)
    it = cs.market_look("it", head_to_head(WEEK, PERS, 23, 7), cal_valid(), primary=PERS)
    assert de["k"] == it["k"] == 1
    assert de["r1"][RANK] == "BAR MET" and it["r1"][WEEK] == "BAR MET"


def test_r1_needs_pooled_capture_at_least_the_primarys():
    # Rankblend wins the sign test (30 tiny wins) but two large losses put its
    # pooled capture below the primary's: the bar is NOT met.
    rows = head_to_head(RANK, PERS, 30, 0, win_by=0.01)
    rows += [row(day(40), RANK, -900.0), row(day(40), PERS, 50.0),
             row(day(41), RANK, -900.0), row(day(41), PERS, 50.0)]
    look = cs.market_look("de", rows, cal_valid(), primary=PERS)
    r1 = comp(look, "R1", RANK, PERS)
    assert r1["significant"] and r1["pooled_capture_a"] < r1["pooled_capture_b"]
    assert look["r1"][RANK].startswith("sign test met, pooled capture below")


# ---- clauses 1 and 7: R7 --------------------------------------------------------

def _three_way(days, *, start=0):
    """PERS and CLIM tie every day; WEEK loses to both every day."""
    out = []
    for i in range(start, start + days):
        out += [row(day(i), PERS, 50.0), row(day(i), CLIM, 50.0), row(day(i), WEEK, 10.0)]
    return out


def test_r7_retires_a_shadow_beaten_by_the_primary_and_by_another_shadow():
    look = cs.market_look("de", _three_way(60), cal_valid(), primary=PERS)
    assert comp(look, "R7", PERS, WEEK)["significant"]
    assert comp(look, "R7", CLIM, WEEK)["significant"]
    assert look["r7"][WEEK] == "DOMINATED"
    # the family holds the R1 and R7 comparisons that meet their preconditions:
    # weekly-beats-primary, primary-beats-weekly, climatology-beats-weekly,
    # weekly-beats-climatology. Climatology vs primary is all ties: n = 0.
    assert look["k"] == 4
    assert look["r7"][CLIM].startswith("not at its precondition (60 of 60 shared days, 0 of 30")
    assert look["r1"][WEEK] == "sign test not met"


def test_r7_needs_60_shared_days():
    look = cs.market_look("de", _three_way(59), cal_valid(), primary=PERS)
    assert not comp(look, "R7", PERS, WEEK)["eligible"]      # 59 shared: one short
    assert look["r7"][WEEK].startswith("not at its precondition (59 of 60 shared days")
    assert look["k"] == 1                                     # only R1 weekly-beats-primary


def test_r7_needs_both_the_primary_and_another_shadow():
    # Weekly loses to the primary on 60 days, but there is no other shadow.
    rows = [r for r in _three_way(60) if r["strategy"] != CLIM]
    look = cs.market_look("de", rows, cal_valid(), primary=PERS)
    assert comp(look, "R7", PERS, WEEK)["significant"]
    assert look["r7"][WEEK] == "not dominated"


# ---- clause 3: a market whose primary does not commit ---------------------------

def test_no_evaluation_where_the_primary_does_not_commit():
    # GB today: only climatology and weekly settle. No R1, no R7, no family.
    look = cs.market_look("gb", head_to_head(CLIM, WEEK, 40, 0), cal_valid(), primary=PERS)
    assert not look["evaluated"] and look["comparisons"] == [] and look["k"] == 0
    assert "does not commit" in "\n".join(cs.render_decision("gb", look, None))
    assert "does not commit" in "\n".join(cs.render_counts("gb", look, None))


def test_the_primary_is_read_from_the_strategy_panel():
    assert cs.primary_strategy() == cs.loop.STRATEGIES[0]["strategy"] == PERS


# ---- clause 2: the confirmatory pair is never in a family -----------------------

def test_es_climatology_vs_persistence_is_outside_the_family_but_not_elsewhere():
    rows = head_to_head(CLIM, PERS, 30, 0)
    es = cs.market_look("es", rows, cal_valid(), primary=PERS)
    r1 = comp(es, "R1", CLIM, PERS)
    assert r1["confirmatory"] and not r1["eligible"] and es["k"] == 0
    assert es["r1"][CLIM] == "decided by the confirmatory comparison, not here"
    # the same pair in any other market is an ordinary exploratory comparison
    de = cs.market_look("de", rows, cal_valid(), primary=PERS)
    assert not comp(de, "R1", CLIM, PERS)["confirmatory"] and de["r1"][CLIM] == "BAR MET"


def test_es_reverse_direction_is_an_r7_comparison_in_the_family():
    """Clause 1 lists "the primary beats S" for every shadow S. The confirmatory
    comparison is the registered one-sided hypothesis (climatology beats
    persistence); the reverse direction is read here as an ordinary R7 member.
    (This reading is recorded for the operator in the register.)"""
    es = cs.market_look("es", head_to_head(PERS, CLIM, 60, 0), cal_valid(), primary=PERS)
    r7 = comp(es, "R7", PERS, CLIM)
    assert not r7["confirmatory"] and r7["eligible"] and r7["significant"] and es["k"] == 1


# ---- clause 5: the confirmatory terminal look ------------------------------------

AUG = date(2026, 8, 1)


def test_confirmatory_has_no_verdict_before_its_90th_non_tied_day():
    rows = head_to_head(CLIM, PERS, 80, 9, ties=30, settled=BEFORE, start=AUG)
    conf = cs.confirmatory_look(rows, cal_valid({90: surv(90)}))
    assert (conf["n"], conf["reached"], conf["p_eff"], conf["met"]) == (89, False, None, None)
    assert "no verdict" in cs._render_confirmatory(conf)


def test_confirmatory_is_decided_on_exactly_its_first_90_non_tied_days():
    # First 90 non-tied days: 60 wins, 30 losses. Then 30 more days, all losses.
    # The verdict is taken on the first 90 and never changes afterwards.
    first = head_to_head(CLIM, PERS, 60, 30, ties=0, settled=BEFORE, start=AUG)
    later = head_to_head(CLIM, PERS, 0, 30, day0=90, start=AUG)
    cal = cal_valid({90: surv(90)})
    at_90 = cs.confirmatory_look(first, cal)
    much_later = cs.confirmatory_look(first + later, cal)
    for conf in (at_90, much_later):
        assert (conf["n"], conf["wins"], conf["losses"]) == (90, 60, 30)
        assert conf["p_eff"] == pytest.approx(cs.sign_test_p(60, 90)) and conf["met"] is True
        assert conf["last_day"] == day(89, AUG)
    assert much_later["days_so_far"] == 120
    assert "BAR MET — final" in cs._render_confirmatory(much_later)


def test_confirmatory_uses_rows_settled_before_the_amendment_and_0_05_undivided():
    # 54 of 90: p_iid = 0.036 — under 0.05 (met) though far over 0.05/12. The
    # one confirmatory look is not split (clause 5), and it runs from 2026-08-01
    # by its own terms, so rows settled before the amendment count.
    rows = head_to_head(CLIM, PERS, 54, 36, settled=BEFORE, start=AUG)
    conf = cs.confirmatory_look(rows, cal_valid({90: surv(90)}))
    assert conf["met"] is True and 0.05 / 12 < conf["p_eff"] < 0.05
    # the frozen table can harden it past 0.05
    hard = cs.confirmatory_look(rows, cal_valid({90: surv(90, {54: 0.06})}))
    assert hard["met"] is False and "BAR NOT MET — final" in cs._render_confirmatory(hard)
    # days before 2026-08-01 are not part of it
    early = head_to_head(CLIM, PERS, 5, 0, settled=BEFORE, start=date(2026, 7, 20))
    assert cs.confirmatory_look(rows + early, cal_valid({90: surv(90)}))["wins"] == 54


def test_confirmatory_as_of_can_place_the_look_before_the_90th_day():
    rows = head_to_head(CLIM, PERS, 60, 30, settled=BEFORE, start=AUG)
    for i, r in enumerate(rows):                      # one day settles per day
        r["settled_at"] = (datetime(2026, 8, 2, 16, tzinfo=timezone.utc)
                           + timedelta(days=i // 2)).isoformat()
    cut = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert cs.confirmatory_look(rows, cal_valid(), as_of=cut)["reached"] is False


# ---- what is printed, and when ---------------------------------------------------

def _cli(monkeypatch, tmp_path, ledgers, *, cal=None, markets=None):
    for slug, rows in ledgers.items():
        (tmp_path / slug).mkdir(exist_ok=True)
        (tmp_path / slug / "ledger.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(cs, "DATA", tmp_path)
    monkeypatch.setattr(cs, "MARKETS", {s: None for s in (markets or ledgers)})
    _use_calibration(monkeypatch, tmp_path, cal or cal_valid({90: surv(90)}))


DECISIVE = head_to_head(RANK, PERS, 30, 0)        # would meet R1 at a decision look


def test_between_looks_only_counts_are_shown(monkeypatch, tmp_path, capsys):
    """Clause 4: no p-value and no verdict outside a decision look — even when
    the data would be decisive. (The digest printing a p-value every day was
    itself a daily look.)"""
    es = DECISIVE + head_to_head(CLIM, PERS, 60, 30, settled=BEFORE, start=AUG)
    _cli(monkeypatch, tmp_path, {"es": es, "gb": head_to_head(CLIM, WEEK, 3, 0)})
    assert cs.main([]) == 0
    out = capsys.readouterr().out
    assert "30W-0T-0L — 30 of 30 non-tied days toward a decision look" in out
    assert "confirmatory, since 2026-08-01: 60W-0T-30L — 90 of 90" in out
    assert "the primary does not commit in this market" in out          # GB
    for forbidden in ("p_eff", "p_iid", "SIGNIFICANT", "BAR MET", "DOMINATED", "0.0"):
        assert forbidden not in out, forbidden
    assert "to reproduce this output later, add: --as-of 2026-11-12T16:00:00+00:00" in out
    assert cs.main(["--panel"]) == 0                 # the old flag still shows counts
    assert "p_eff" not in capsys.readouterr().out


def test_decision_look_prints_family_threshold_and_verdicts(monkeypatch, tmp_path, capsys):
    es = DECISIVE + head_to_head(CLIM, PERS, 60, 30, settled=BEFORE, start=AUG)
    _cli(monkeypatch, tmp_path, {"es": es})
    assert cs.main(["--decision-look"]) == 0
    out = capsys.readouterr().out
    assert "DECISION LOOK" in out and "is the frozen file" in out
    assert "per-look threshold 0.05/12 = 0.004167" in out
    assert "family: k = 1" in out
    assert "1. R1 rankblend beats persistence: n=30 (30W-0T-0L, 30 shared" in out
    assert "vs 0.004167 -> SIGNIFICANT" in out
    assert "R1 rankblend: BAR MET (pooled capture 60.0% vs 50.0%)" in out
    assert "R7 rankblend: not at its precondition" in out
    assert "not in the family (precondition not met; counts only):" in out
    assert "first 90 non-tied days" in out and "BAR MET — final" in out


def test_decision_look_with_nothing_eligible_decides_nothing(monkeypatch, tmp_path, capsys):
    _cli(monkeypatch, tmp_path, {"de": head_to_head(RANK, PERS, 5, 0)})
    assert cs.main(["--decision-look", "--market", "de"]) == 0
    out = capsys.readouterr().out
    assert "family: k = 0 — no comparison meets its precondition" in out
    assert "SIGNIFICANT" not in out and "BAR MET" not in out


def test_decision_look_flags_two_shadows_meeting_r1(monkeypatch, tmp_path, capsys):
    rows = DECISIVE + [r for r in head_to_head(WEEK, PERS, 30, 0) if r["strategy"] == WEEK]
    _cli(monkeypatch, tmp_path, {"de": rows})
    assert cs.main(["--decision-look"]) == 0
    assert "more than one shadow meets R1" in capsys.readouterr().out


def test_decision_look_refuses_without_the_frozen_calibration(monkeypatch, tmp_path, capsys):
    _cli(monkeypatch, tmp_path, {"de": DECISIVE})
    monkeypatch.setattr(cs, "CALIBRATION_SHA256", "0" * 64)       # not the frozen file
    assert cs.main(["--decision-look"]) == 2
    out = capsys.readouterr().out
    assert out.startswith("NO VERDICT") and "BAR MET" not in out
    assert cs.main([]) == 0                                        # counts still work


def test_as_of_and_missing_ledger_on_the_cli(monkeypatch, tmp_path, capsys):
    _cli(monkeypatch, tmp_path, {"de": DECISIVE}, markets=["de", "pt"])
    assert cs.main(["--as-of", "2026-11-11T12:00:00"]) == 0       # before any settlement
    out = capsys.readouterr().out
    assert "0W-0T-0L — 0 of 30" in out and "PT  no ledger in this checkout" in out
    assert "to reproduce" not in out


def test_single_pair_shows_counts_only(monkeypatch, tmp_path, capsys):
    es = head_to_head(RANK, PERS, 40, 18, ties=34, settled=BEFORE, start=AUG) + \
        head_to_head(RANK, PERS, 2, 1)
    _cli(monkeypatch, tmp_path, {"es": es})
    assert cs.main([RANK, PERS]) == 0
    out = capsys.readouterr().out
    assert "all settled days: 95 shared, 42W-34T-19L" in out
    assert "since the amendment (2026-11-11T03:30Z): 3 shared, 2W-0T-1L" in out
    assert "p_" not in out
    assert cs.main([RANK, WEEK, "--market", "es"]) == 1
    assert "no shared settled days" in capsys.readouterr().out


def test_cli_rejects_one_strategy_or_an_unknown_market(monkeypatch, tmp_path):
    _cli(monkeypatch, tmp_path, {"es": DECISIVE})
    for argv in ([RANK], ["--market", "atlantis"]):
        with pytest.raises(SystemExit):
            cs.main(argv)
