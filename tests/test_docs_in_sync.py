"""Documentation-drift detectors (closed defect loop: doc staleness is a
failure class, so it gets a regression test). These FAIL the build when a
market or strategy is added in code but not named in the docs — which is
exactly how ARCHITECTURE.md fell behind the multi-market build.

Deliberately cheap: they check that each code-registered name APPEARS in the
canonical doc, not that prose is worded a particular way. Live numbers are NOT
checked here — those belong in the ledger/status/dashboard, never frozen in a
doc."""
import functools
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import is_private_repo, private_file, published_file
from talea.loop import STRATEGIES
from talea.markets import MARKETS

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = (ROOT / "docs" / "ARCHITECTURE.md").read_text()


# Files only the private repository has are read when a test asks for them, not
# when this module is imported: on the public mirror each such test is skipped
# on its own, with the file it needs named, and the rest of this file runs.
@functools.cache
def _private_text(rel: str) -> str:
    return private_file(rel).read_text()


def test_every_market_is_documented():
    """Each registered market slug must appear (as an upper-case token) in the
    ARCHITECTURE markets inventory."""
    missing = [slug for slug in MARKETS
               if not re.search(rf"\b{slug.upper()}\b", ARCHITECTURE)]
    assert not missing, (
        f"markets in code but not in docs/ARCHITECTURE.md: {missing} — "
        f"update the Markets table when you register a market")


def test_every_strategy_is_documented():
    """Each registered strategy's short name must appear in BOTH the
    ARCHITECTURE panel and CLAUDE.md's registered panel."""
    names = [s["strategy"].replace("battery-2h2h-", "") for s in STRATEGIES]
    missing_arch = [n for n in names if n not in ARCHITECTURE]
    assert not missing_arch, (
        f"strategies in code but not in docs/ARCHITECTURE.md panel: {missing_arch}")
    # ARCHITECTURE.md is published, so the half above runs on a public copy too;
    # only what follows needs the private file (and is skipped there, by name).
    missing_claude = [n for n in names if n not in _private_text("CLAUDE.md")]
    assert not missing_claude, (
        f"strategies in code but not in CLAUDE.md registered panel: {missing_claude}")


def _architecture_market_rows():
    """The rows of the ARCHITECTURE.md Markets table, keyed by slug."""
    body = ARCHITECTURE.split("## Markets", 1)[1].split("\n## ", 1)[0]
    slugs = {s.upper() for s in MARKETS}
    return {mm.group(1).lower(): line for line in body.splitlines()
            if (mm := re.match(r"\s+([A-Z]{2,6})\b", line)) and mm.group(1) in slugs}


def test_architecture_table_lists_exactly_the_registered_markets():
    assert set(_architecture_market_rows()) == set(MARKETS)


def test_architecture_table_flags_match_the_registry():
    rows = _architecture_market_rows()
    for slug, m in MARKETS.items():
        line = rows[slug]
        assert ("private" in line) != m.public, f"{slug} public flag drift: {line}"
        assert ("Actions" in line) == (m.driver == "actions"), \
            f"{slug} driver flag drift: {line}"


def test_verify_discloses_migrated_market_evidence_boundaries():
    """Incident 2026-08-29 (independent auditor): consolidating markets into the
    one `talea` repo makes a migrated market's git-history in THIS repo begin with
    a bulk sync, so the timing 'weak check' (committed_at vs the commit that
    introduced a receipt) cannot corroborate its pre-migration dates from this repo
    alone. VERIFY.md MUST disclose that boundary for every migrated-in public
    market — as it already does for ES's pre-2026-08-10 mirror bulk import — or the
    'git-attested' label overclaims. Guards the ES and DE disclosures against being
    dropped in a future VERIFY rewrite.

    A second, same-day audit pass found the identical gap for ERCOT: its receipts
    also entered this repo in the single fd4bdd49 consolidation commit, but
    VERIFY.md's per-market tier section disclosed the boundary only for DE, not
    ERCOT (which it instead described as plain 'git-attested from their first
    tick' — the overclaim). Guard that disclosure too."""
    v = (ROOT / "VERIFY.md").read_text().lower()
    assert "2026-08-10" in v and "bulk" in v, \
        "VERIFY.md must disclose ES's pre-2026-08-10 bulk-import evidence boundary"
    assert "evidence boundary" in v, \
        "VERIFY.md must name the evidence-boundary concept for migrated markets"
    assert "germany-dayahead-ledger" in v or "consolidation" in v, \
        ("VERIFY.md must disclose DE's evidence boundary — its talea git-history "
         "begins with the 2026-08-29 consolidation sync, per-tick history lives "
         "in the private repo / frozen germany-dayahead-ledger mirror")
    assert "- **ercot**" in v, "VERIFY.md must give ERCOT its own tier bullet"
    ercot_bullet = v.split("- **ercot**", 1)[1].split("\n- **", 1)[0]
    assert "fd4bdd49" in ercot_bullet and "evidence boundary" in ercot_bullet, \
        ("VERIFY.md's ERCOT bullet must disclose the same consolidation-commit "
         "evidence boundary as DE's — not just describe ERCOT as plain "
         "'git-attested from their first tick'")


def test_verify_discloses_the_2026_11_server_sync_outage_boundary():
    """Incident 2026-11-09 (server clone diverged 2026-11-04 → 11-10): ERCOT's
    receipts for targets 2026-11-05 → 11-11 reached the public mirror in ONE
    late commit (7f05f41), after every one of those targets had published — the
    same weak-check blind spot as the 2026-08-29 bulk imports, so VERIFY.md must
    disclose it the same way (and say what DOES corroborate the window). Guards
    the disclosure against being dropped in a future VERIFY rewrite."""
    v = (ROOT / "VERIFY.md").read_text().lower()
    ercot_bullet = v.split("- **ercot**", 1)[1].split("\n- **", 1)[0]
    assert "7f05f41" in ercot_bullet and "2026-11-05" in ercot_bullet \
        and "2026-11-11" in ercot_bullet, \
        ("VERIFY.md's ERCOT bullet must disclose the 2026-11 outage boundary: "
         "targets 2026-11-05 → 11-11 entered the mirror in the single late "
         "commit 7f05f41")
    assert "data/ercot/ots/2026-11-04" in ercot_bullet, \
        "the disclosure must point at the on-time OTS manifests that cover the window"
    assert "server-sync outage" in v.split("## 2.", 1)[0].split("## 1.", 1)[1], \
        ("section 1 must state what the outage did to the weak check for ES/DE/GB "
         "and to the private repo's commit times")


def test_every_public_market_has_a_colocated_data_licence():
    """Licensing travels with the data: each PUBLISHED market carries its own
    Data/<slug>/LICENSE.md (source + licence + attribution) and is indexed in
    DATA-SOURCES.md. A public market missing its licence file could redistribute
    third-party price data with no attribution — a licence breach. (Private
    markets are never published, so they need no public licence file.)"""
    from talea.markets import public_markets
    index = (ROOT / "DATA-SOURCES.md").read_text()
    for m in public_markets():
        lic = ROOT / "Data" / m.slug / "LICENSE.md"
        assert lic.exists(), \
            f"public market {m.slug!r} has no Data/{m.slug}/LICENSE.md"
        assert f"Data/{m.slug}/" in index, \
            f"{m.slug!r} is public but missing from the DATA-SOURCES.md index"


def test_infra_md_names_every_actions_driven_market():
    """Incident 2026-09-01 (infra-continuity audit, second pass): JP started
    ticking via GitHub Actions the same day the ERCOT cron was fixed, but
    INFRA.md's System map GitHub Actions writer line was never updated to
    name it — it still read 'ERCOT tick only'. Guard the writer inventory:
    every market registered with driver == "actions" must be named (by
    upper-case slug) on the System map's GitHub Actions line, so a future
    market added on this driver can't silently drop off the recovery doc."""
    INFRA = _private_text("INFRA.md")
    line = next(l for l in INFRA.splitlines() if "GitHub Actions" in l
                and "SCHEDULERS" not in l)
    # The writer block wraps onto following indented lines until a blank one.
    start = INFRA.index(line)
    block = INFRA[start:start + INFRA[start:].index("\n\n")]
    actions_markets = [slug for slug, m in MARKETS.items() if m.driver == "actions"]
    missing = [slug for slug in actions_markets
               if not re.search(rf"\b{slug.upper()}\b", block)]
    assert not missing, (
        f"markets driven by GitHub Actions but not named on INFRA.md's "
        f"System map GitHub Actions line: {missing}")


def test_system_map_deadline_table_matches_the_registry():
    """docs/system-map.html is a DERIVED, hand-regenerated infra map; its per-market
    publication-deadline table is the one part that maps directly onto machine-readable
    config (the market registry), so it earns a cheap deterministic guard on EVERY CI
    run instead of only the quarterly constitutional auditor. Asserts the table lists
    EXACTLY the registered markets, and that each row's visibility, clock-guard cutoff
    (deadline_hour + tz label), local zone, and writer match the registry. The rest of
    the map (schedule / topology / flow / cloud-routine crons) stays auditor-guarded —
    those sources are not machine-readable in this repo, so generation can't cover them."""
    html = _private_text("docs/system-map.html")
    section = html.split("Markets — publication deadlines", 1)[1].split("Schedule —", 1)[0]
    market_rows = {}
    for inner in re.findall(r"<tr>(.*?)</tr>", section, re.S):
        m = re.match(r"\s*<td>([A-Z]{2,6}) ·", inner)   # data rows only (header uses <th>)
        if m:
            market_rows[m.group(1).lower()] = inner
    assert set(market_rows) == set(MARKETS), (
        f"system-map deadline table lists {set(market_rows)} but the registry has "
        f"{set(MARKETS)} — add/remove the market row when the registry changes")
    for slug, m in MARKETS.items():
        row = market_rows[slug]
        tzlabel = m.presentation.tz_label
        vis = "public" if m.public else "private"
        writer = "Actions" if m.driver == "actions" else "VPS"
        assert f"{m.deadline_hour}:00 {tzlabel}" in row, \
            f"{slug}: commit-by cell must read '{m.deadline_hour}:00 {tzlabel}' (from the registry)"
        assert f">{vis}<" in row, f"{slug}: visibility must be {vis} (registry public={m.public})"
        assert f">{writer}<" in row, f"{slug}: writer tag must be {writer} (registry driver={m.driver})"
        assert str(m.tz) in row, f"{slug}: local zone must be {m.tz}"


def test_enumerating_docs_list_every_routine_mirror():
    """Enumeration drift (recurring — hit 3x over 2026-08-30..09-02): adding a
    cloud routine + its docs/routines/ mirror WITHOUT updating the docs that
    ENUMERATE the routines left them stale (INFRA.md said "SEVEN" and omitted 3
    resolvers; docs/system-map.html's stat tile read 7). The set of routine
    MIRRORS (docs/routines/*.md files carrying a '## Prompt (verbatim)' block) is
    the single source of truth; every enumerating doc must agree with it. This
    converts "I forgot to update the count" from a recurring agent-session catch
    into a CI failure — so it's machinery-caught before commit, not an escape."""
    routines = private_file("docs/routines")
    mirrors = [p for p in sorted(routines.glob("*.md"))
               if "## Prompt (verbatim)" in p.read_text()]
    assert len(mirrors) >= 5, "expected the standing routine mirrors to be present"
    infra = _private_text("INFRA.md")
    readme = (routines / "README.md").read_text()
    for p in mirrors:
        assert p.stem in infra, (
            f"routine mirror {p.stem!r} is not named in INFRA.md's cloud-routines "
            f"map — update the MIRRORS list + the count when you add/remove a routine")
        assert p.stem in readme, (
            f"routine mirror {p.stem!r} is not named in docs/routines/README.md")
    smap = _private_text("docs/system-map.html")
    m = re.search(r'<div class="n">(\d+)</div><div class="l">standing routines', smap)
    assert m, "docs/system-map.html must carry a '<n> standing routines' stat tile"
    assert int(m.group(1)) == len(mirrors), (
        f"system-map stat tile says {m.group(1)} standing routines but there are "
        f"{len(mirrors)} routine mirrors — update the tile AND add the schedule row")


def test_incident_escape_tally_matches_the_table():
    """Incident 2026-08-30: the escape-rate summary silently drifted to a stale
    '6 of 11' — seeded 2026-08-21 and never recounted as ~11 later incidents were
    appended, so the headline metric understated both the incident count AND the
    escapes. The escape rate is the closed-defect-loop's own arrival metric, so a
    wrong tally is itself a governance defect (and it already bit once). This guard
    recomputes the tally from the table — total incident rows, and rows whose
    'noticed first by' column (3rd cell) marks an ESCAPE — and asserts the
    '**N of M incidents were escapes**' summary matches both. A future resolver
    that appends a row but forgets to bump the number now fails CI, not silently."""
    txt = (ROOT / "docs" / "incidents.md").read_text()
    rows = [l for l in txt.splitlines() if re.match(r"\|\s*\d{4}-\d\d", l)]
    assert rows, "no incident rows found — has the table format changed?"
    # each row must be a clean 5-column markdown row so the positional
    # 'noticed first by' cell is unambiguous (a stray pipe in a cell would misparse)
    for l in rows:
        assert l.count("|") == 6, \
            f"incident row is not a clean 5-column row (stray pipe in a cell?): {l[:90]}"
    total = len(rows)
    escapes = sum(1 for l in rows if "ESCAPE" in l.split("|")[3])  # cell 3 = noticed-first-by
    m = re.search(r"\*\*(\d+) of (\d+) incidents were escapes\*\*", txt)
    assert m, "incidents.md must carry a '**N of M incidents were escapes**' summary line"
    stated_esc, stated_total = int(m.group(1)), int(m.group(2))
    assert (stated_esc, stated_total) == (escapes, total), (
        f"escape-rate tally drift: summary says {stated_esc} of {stated_total}, but the "
        f"table has {escapes} escapes of {total} incidents — recount and update the line")


def _alert_channel_rows(infra: str) -> list[list[str]]:
    """The rows of INFRA.md's alert-channel table, as lists of cells."""
    head = "### Alert channels"
    assert head in infra, "INFRA.md must keep its 'Alert channels' table"
    rows = []
    for line in infra.split(head, 1)[1].splitlines():
        if line.startswith("#"):
            break
        if line.startswith("|") and not line.startswith("|---") \
                and not line.startswith("| Channel"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def _alert_channel_problems(rows: list[list[str]], senders: dict[str, bool]) -> list[str]:
    """What is wrong with the table: a row with no receipt status, or a channel
    the code sends on that has no row. `senders` maps a word that must appear in
    some row's channel cell to whether the code sends on that channel."""
    status = re.compile(r"^(\d{4}-\d\d-\d\d|UNCONFIRMED|NOT RECEIVED|RETIRED)\b")
    problems = [f"row {r[0]!r} does not say whether the operator confirmed it"
                for r in rows if len(r) != 3 or not status.match(r[2])]
    problems += [f"the code sends on {word!r} but the table has no row for it"
                 for word, sends in senders.items()
                 if sends and not any(word in r[0].lower() for r in rows)]
    return problems


def test_every_alert_channel_states_whether_the_operator_confirmed_it():
    """Incident 2026-11-11: the ntfy phone push had never reached the operator.
    It was the only liveness channel for five weeks and fired 21 times in one
    week, and nothing noticed, because the send (`curl ... || true`) cannot fail
    and nobody had ever checked the receiving end. A sender that runs is not a
    channel that works. INFRA.md therefore lists every alert channel with the
    date the operator last confirmed receiving one, or UNCONFIRMED, or NOT
    RECEIVED, or RETIRED — and a channel the code sends on cannot be missing
    from it. (The push itself was retired the same day; the routine briefs
    still name it, so its row stays.)"""
    tick = (ROOT / "scripts" / "server_tick.sh").read_text()
    shell = (ROOT / "src" / "talea" / "__main__.py").read_text()
    routines = "".join(p.read_text() for p in (ROOT / "docs" / "routines").glob("*.md"))
    senders = {
        "ntfy": "ntfy.sh" in tick or "ntfy.sh" in routines,
        "heartbeat": "ESIOS_HEARTBEAT_URL" in shell,
        "email": "smtplib" in shell,
    }
    assert any(senders.values()), "no sender found — has the alert code moved?"
    rows = _alert_channel_rows(_private_text("INFRA.md"))
    assert not _alert_channel_problems(rows, senders), _alert_channel_problems(rows, senders)

    # the guard must be able to fail the way the real defect did
    assert _alert_channel_problems([["ntfy phone push", "stale market", "sent on every tick"]],
                                   {"ntfy": True})          # "sent" is not "received"
    assert _alert_channel_problems([r for r in rows if "ntfy" not in r[0].lower()],
                                   {"ntfy": True})          # a sender with no row
    assert not _alert_channel_problems([["sms", "x", "UNCONFIRMED — never drilled"]],
                                       {"ntfy": False})     # honest "unknown" passes


def test_mirror_is_deny_by_default_allowlist():
    """Incident 2026-08-28: FR (private, derived-only) leaked to the PUBLIC mirror
    through a DENY-LIST gap — fr/ was never added to the excludes. The Talea mirror
    (one project, all public markets under Data/<slug>/) must be a POSITIVE
    ALLOWLIST (deny-by-default): a terminal --exclude='*' so anything not named
    cannot leak, per-market includes REGISTRY-DERIVED (a loop over
    `markets --public`) so no PRIVATE market is ever named, and a fail-safe that
    skips publishing when the lookup is empty (so a bad lookup can never
    --delete-excluded the whole mirror). Leaking a private market now requires
    flagging it public in the registry, not forgetting an exclude."""
    from talea.markets import public_markets
    # The list lives in ONE script, shared by the publisher and by CI.
    txt = (ROOT / "scripts" / "mirror_tree.sh").read_text()
    publish = "\n".join(l for l in (ROOT / "scripts" / "publish_mirror.sh").read_text().splitlines()
                        if not l.lstrip().startswith("#"))
    assert 'bash scripts/mirror_tree.sh "$MIRROR"' in publish, \
        "publish_mirror.sh must build the mirror's file set with scripts/mirror_tree.sh"
    assert not re.search(r"\b(rsync|cp)\b|--include", publish), \
        "publish_mirror.sh must not copy anything itself: the list exists once, in mirror_tree.sh"
    assert 'if [ -z "$SLUGS" ]' in publish, \
        "publish_mirror.sh must skip (publish nothing) when no public market is resolved"
    assert "--exclude='*'" in txt, \
        "mirror publish must end in a deny-all --exclude='*' (allowlist posture)"
    assert "for s in $SLUGS" in txt and '--include="/$s/***"' in txt, \
        ("public-market includes must be REGISTRY-DERIVED (loop over "
         "`markets --public`), not hardcoded per market")
    assert 'if [ -z "$SLUGS" ]' in txt, \
        "mirror publish must fail-safe (skip) when the public-market lookup is empty"
    public = {m.slug for m in public_markets()}
    for slug in MARKETS:
        if slug in public:
            continue
        assert f"--include='/{slug}/" not in txt and f'--include="/{slug}/' not in txt, \
            (f"{slug!r} is PRIVATE but explicitly --include'd in the mirror "
             f"allowlist — a private market must never be published.")


_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def test_architecture_market_count_words_match_the_registry():
    """Audit finding D2 (2026-09-23): ARCHITECTURE.md said "eight markets run
    today" in one paragraph and "all seven markets are vertical modules" in
    another — a count that was bumped in one place when JP was registered and
    not the other. Every spelled-out "<N> markets" in the doc must equal the
    registry's size, so the next market registration cannot leave a stale count."""
    counts = {w.lower() for w in re.findall(
        r"\b([A-Za-z]+)\s+markets\b", ARCHITECTURE) if w.lower() in _NUMBER_WORDS}
    assert counts, "expected ARCHITECTURE.md to state the market count in words"
    wrong = sorted(w for w in counts if _NUMBER_WORDS[w] != len(MARKETS))
    assert not wrong, (
        f"docs/ARCHITECTURE.md says '{wrong[0]} markets' but the registry has "
        f"{len(MARKETS)} — update every spelled-out count when a market is added")


def test_public_governance_extract_states_the_bar_in_force():
    """Found 2026-11-11: GOVERNANCE.md, the public extract VERIFY.md sends
    readers to, still stated the comparison bar as "30 non-tied days and
    p < 0.05" — the rule before Option C (2026-08-27) and Amendment 1
    (2026-11-10). The published bar was easier than the bar in force, so an
    outside reader could not check a verdict against it. The extract must carry
    every number the comparison script applies; they are read from the script,
    so changing a rule without updating the public page fails here."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "compare_strategies", ROOT / "scripts" / "compare_strategies.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    gov = " ".join((ROOT / "GOVERNANCE.md").read_text().split())
    needed = {
        "the p-value definition": "p_eff = max(p_iid, p_boot)",
        "the frozen table's hash": cs.CALIBRATION_SHA256,
        "the amendment instant": cs.AMENDMENT_1_AT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "the per-review threshold": "0.05/12",
        "the precondition": f"at least {cs.N_MIN} non-tied days",
        "R7's shared days": f"at least {cs.R7_SHARED_MIN} shared",
        "the terminal look": f"first {cs.CONFIRMATORY['n']} non-tied shared days "
                             f"since {cs.CONFIRMATORY['since']}",
        "the terminal alpha": f"at {cs.CONFIRMATORY['alpha']}",
        "counts only between reviews": "no p-value and no verdict",
        "the non-ES consequence": "no new strategy or escalation there",
    }
    assert abs(cs.LOOK_ALPHA - 0.05 / 12) < 1e-15      # the page says 0.05/12
    missing = [what for what, text in needed.items() if text not in gov]
    assert not missing, f"GOVERNANCE.md does not state: {missing}"
    assert "sign test p < 0.05 on paired" not in gov, "the pre-2026-08-27 bar is back"


def test_published_mirror_files_do_not_name_the_dead_repo():
    """Audit finding D2 (2026-09-23): the workflow shipped to the public mirror
    (mirror/verify.yml) still introduced itself as running on
    'spain-dayahead-ledger', a repo renamed to `talea` on 2026-08-29. Files
    copied VERBATIM onto the mirror by publish_mirror.sh must not carry the dead
    name (incidents.md is exempt: it records history)."""
    for rel, on_mirror in (("mirror/verify.yml", ".github/workflows/verify.yml"),
                           ("README-public.md", "README.md"), ("VERIFY.md", "VERIFY.md"),
                           ("GOVERNANCE.md", "GOVERNANCE.md"),
                           ("DATA-SOURCES.md", "DATA-SOURCES.md"),
                           ("docs/ARCHITECTURE.md", "docs/ARCHITECTURE.md")):
        txt = published_file(rel, on_mirror).read_text()
        assert "spain-dayahead-ledger" not in txt, \
            f"{rel} names the dead repo 'spain-dayahead-ledger' (renamed to talea)"


def test_gb_licence_does_not_call_the_market_index_day_ahead():
    """Independent-audit observation (2026-10-05): Data/gb/LICENSE.md described
    the feed as the 'GB day-ahead Market Index price', contradicting VERIFY.md
    and the page banner (2026-09-07 finding F2: the Elexon Market Index is a
    traded WITHIN-DAY index, not the day-ahead auction). The licence file ships
    on the public mirror next to the data, so its description of the data must
    match the disclosed truth."""
    lic = (ROOT / "Data" / "gb" / "LICENSE.md").read_text()
    flat = " ".join(lic.split())
    assert "day-ahead Market Index" not in flat
    assert "within-day" in flat


def test_public_docs_disclose_that_pnl_does_not_enforce_dispatch_order():
    """Red-team finding 2026-10-01, unanswered for six weeks: the P&L pairs a
    day's sells with its buys regardless of order, so some booked days are
    schedules a battery could not run — and VERIFY.md, the README and the page
    footer all said relative metrics were 'robust'. They are robust to the fee
    assumption only. All three must say so, and VERIFY.md must point at the
    script that measures the effect."""
    verify = (ROOT / "VERIFY.md").read_text()
    readme = published_file("README-public.md", "README.md").read_text()
    footer = (ROOT / "scripts" / "render_dashboard.py").read_text()
    for name, text in (("VERIFY.md", verify), ("README-public.md", readme),
                       ("render_dashboard.py", footer)):
        assert "dispatch order" in text, f"{name} lost the dispatch-order disclosure"
        for bad in ("relative metrics are robust.", "are robust to that. Hourly",
                    "are robust to this."):
            assert bad not in text, f"{name} again claims relative metrics are robust, unqualified"
    assert "scripts/feasibility_report.py" in verify and "NOT robust" in verify
    assert (ROOT / "scripts" / "feasibility_report.py").exists()


README_TEST_COMMAND = "uv sync && uv run pytest"


def test_public_readme_says_what_its_test_command_shows():
    """Incident 2026-11-13: the public README said "Reproduce everything:
    `uv sync && uv run pytest`", and on the mirror that command stopped at
    collection, because nothing ran it there. The README must say that tests
    needing private files skip and name the file, and point at the ledger's own
    re-derivation, not promise that the tests reproduce everything."""
    readme = " ".join(published_file("README-public.md", "README.md").read_text().split())
    assert "Reproduce everything" not in readme
    assert f"Run the tests: `{README_TEST_COMMAND}`." in readme
    assert "Tests that need files kept in the private repository skip and name the file." in readme
    assert "`python scripts/verify_ledger.py --all`" in readme


def test_ci_runs_the_public_readmes_command_in_the_mirrors_tree():
    """The same incident: the README and CI name the same command. This checks
    only that the last line of ci.yml's mirror step is the README's command, so
    that changing one without the other fails here. That the step runs at all,
    and runs that line in the mirror's tree (no `if:`, no `continue-on-error`,
    no dropped `cd`, nothing added), is test_check_append_only.py's: it pins the
    whole step, line by line."""
    ci = _private_text(".github/workflows/ci.yml")
    step = ci.split("- name: The public README's command passes in the tree the mirror gets", 1)
    assert len(step) == 2, "ci.yml lost the step that runs the README's command in the mirror's tree"
    body = step[1].split("\n      - ", 1)[0]
    last = [l.strip() for l in body.splitlines() if l.strip()][-1]
    assert last == README_TEST_COMMAND, \
        f"ci.yml's mirror step runs {last!r}, not the README's command"


# How a mirrored file can name a pull request of the private repository:
# "#<n>", "PR <n>", "PR#<n>", "PRs #<n>", "pull/<n>", "pull request <n>". Any case.
# (This file is mirrored too and is read by the same check, so no real number is
# written in it: the cases below put theirs in at run time.)
PR_NUMBER = re.compile(r"#\d+|\bPRs?\s*#?\d+|\bpull/\d+|\bpull\s+requests?\s*#?\d+", re.I)
# The numbers in mirrored files that are not PRs: (file, the words before the
# "#", the number). Exact file, exact words, exact number.
NOT_A_PR = {
    ("docs/ARCHITECTURE.md", "defect ", 3),
    ("src/talea/markets/_entsoe.py", "defect ", 3),
    ("src/talea/markets/fr/__init__.py", "defect ", 3),
    ("src/talea/markets/__init__.py", "shortlist's ", 1),
    ("src/talea/markets/gb/__init__.py", "shortlist's ", 1),
    ("tests/test_workflow_schedules.py", "finding ", 2),
}
# The one file that writes HTML by hand. In it, two things are not PRs:
# - a CSS colour it uses today that starts with a digit, listed here exactly
#   (without its "#", so that this file names none), and only inside one of
#   the two whole CSS tokens the file writes colours in: a custom property
#   "--name:<colour>;" or a fallback "var(--name,<colour>)". A new colour fails
#   the check until it is added; a "#" and digits anywhere else is not a colour,
#   listed or not ("PR:", "see:", "x," or "margin:" before it are caught).
# - a numeric character reference ("&#" digits ";").
HTML_WRITERS = {"scripts/render_dashboard.py"}
HTML_COLOURS = frozenset({"006300", "0b0b0b", "0ca30c", "0d0d0d", "111", "1a1a19", "2a78d6",
                          "2c2c2a", "383835", "3987e5", "52514e", "666", "898781"})
HEX_RUN = re.compile(r"#([0-9a-fA-F]+)(?![0-9A-Za-z_])")
# A limit, stated: a whole token is exempt wherever it stands, even in a comment
# or prose; only its shape is checked, not that it is CSS.
CSS_BEFORE = {";": re.compile(r"(?<![\w-])--[a-z0-9-]+:\Z"),
              ")": re.compile(r"(?<![\w-])var\(--[a-z0-9-]+,\Z")}
ENTITY = re.compile(r"#\d+;")
# Not read: the pages scripts/render_dashboard.py generates at the mirror's
# root (their CSS colours look like "#<digits>"; the script that writes them is
# read), named exactly, and files that are not UTF-8 text. Every other .html is
# read: a recorded upstream page kept as a test fixture is copied verbatim.


def _generated_pages() -> set[str]:
    """index.html for the primary market, <slug>.html for each other public one
    (render_dashboard.py's _href), at the root only. An assumption, by name: a
    file at one of those paths is taken to be the renderer's page. Nothing here
    checks that its content came from the renderer."""
    from talea.markets import public_markets
    return {"index.html"} | {f"{m.slug}.html" for m in public_markets() if not m.primary}


def _pr_numbers(rel: str, text: str) -> list[str]:
    """Each PR reference in `text`, as "<rel>:<line>: <match>"."""
    found = []
    for m in PR_NUMBER.finditer(text):
        if any(rel == f and m.group() == f"#{n}" and text.endswith(words, 0, m.start())
               for f, words, n in NOT_A_PR):
            continue
        if rel in HTML_WRITERS:
            hex_run = HEX_RUN.match(text, m.start())
            if hex_run and hex_run.group(1).lower() in HTML_COLOURS:
                before = CSS_BEFORE.get(text[hex_run.end():hex_run.end() + 1])
                if before and before.search(text, 0, m.start()):
                    continue
            if text[m.start() - 1:m.start()] == "&" and ENTITY.match(text, m.start()):
                continue
        found.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group()}")
    return found


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


# The lines of scripts/mirror_tree.sh that copy a NAMED .md file to the mirror.
_TOP = re.compile(r'^cp (VERIFY\.md [^\n]*) "\$MIRROR/"$', re.M)
_LOOP = re.compile(r"^for d in ([^;]*); do$", re.M)
_README = re.compile(r'^cp README-public\.md "\$MIRROR/README\.md"$', re.M)
_MOVED = re.compile(r"^  --include='/README-MOVED\.md' \\$", re.M)


def _named_documents(tree: str) -> list[str]:
    """The .md files `tree` (the text of scripts/mirror_tree.sh) copies by name,
    as paths in this repository. Every line of code in it that names a .md file
    must be one of the four forms read here, so a document copied some new way
    fails this test instead of going unchecked."""
    top, loop, readme, moved = (r.search(tree) for r in (_TOP, _LOOP, _README, _MOVED))
    assert top and loop and readme and moved, \
        "scripts/mirror_tree.sh no longer copies documents the way this test reads"
    read = set()
    for m in (top, loop, readme, moved):
        first = tree.count("\n", 0, m.start())
        read.update(range(first, first + m.group().count("\n") + 1))
    unread = [l for i, l in enumerate(tree.splitlines())
              if i not in read and not l.lstrip().startswith("#") and re.search(r"\.md\b", l)]
    assert not unread, f"scripts/mirror_tree.sh names .md files in lines this test does not read: {unread}"
    names = top.group(1).split()
    names += ["docs/" + n for n in loop.group(1).replace("\\", " ").split()]
    return ["README-public.md", "Data/README-MOVED.md", "Data/esios_prices.json"] + names


def _mirrored_documents() -> list[tuple[str, Path]]:
    """The files the mirror gets, as far as this repository can tell without
    building it: the ones scripts/mirror_tree.sh names, and every file inside
    the trees it copies whole (src/, scripts/, tests/, Data/calibration/ and each
    public market's Data/<slug>/), plus Data/esios_prices.json. The guard above
    covers named .md files only; CI's scan of the built tree is the guarantee."""
    from talea.markets import public_markets
    names = _named_documents((ROOT / "scripts" / "mirror_tree.sh").read_text())
    assert "docs/incidents.md" in names and "VERIFY.md" in names
    docs = [(n, published_file(n, "README.md") if n == "README-public.md" else ROOT / n) for n in names]
    trees = ["src", "scripts", "tests", "Data/calibration"] + [f"Data/{m.slug}" for m in public_markets()]
    for d in trees:
        docs += [(p.relative_to(ROOT).as_posix(), p) for p in sorted((ROOT / d).rglob("*"))
                 if p.is_file() and "__pycache__" not in p.parts]
    # Copied by name without ".md": the licence (if present) and the mirror's workflow.
    docs += [(n, ROOT / n) for n in ("LICENSE",) if (ROOT / n).exists()]
    docs += [("mirror/verify.yml", ROOT / "mirror" / "verify.yml")]
    return docs


def test_mirrored_documents_name_no_private_pr_number():
    """Review finding 2026-11-14 (Codex): docs/incidents.md is copied verbatim to
    the public mirror and four of its rows held eleven references, by number, to
    pull requests of the private repository. A private PR number means nothing to a public
    reader and points into a repository they cannot open. Each mirrored file
    must describe the change in words instead. Round 2 of the same review found
    this test file itself, mirrored with tests/, quoting the very numbers.

    Every UTF-8 text file is read, not only documents, except the pages the
    renderer generates (_generated_pages). Two places, two lists. On a public copy (no
    PRIVATE-REPO) the checkout IS the published tree, so every file in it is
    read, however it got there: CI runs this in the tree scripts/mirror_tree.sh
    builds, on every push. Here, in the private repository, the list is what
    mirror_tree.sh names plus the trees it copies whole: a faster check before
    CI, not the guarantee."""
    files = _mirrored_documents() if is_private_repo() else _published_files(ROOT)
    rels = {rel for rel, _ in files}
    assert {"docs/incidents.md", "tests/test_docs_in_sync.py"} <= rels, \
        "the list of mirrored files is missing docs/incidents.md or this test file: it was not read"
    found = _scan(files)
    assert not found, f"mirrored files name a PR number: {found}"


def _scan(files: list[tuple[str, Path]]) -> list[str]:
    generated = _generated_pages()
    return [hit for rel, path in files
            if rel not in generated and (text := _text(path)) is not None
            for hit in _pr_numbers(rel, text)]


def test_only_the_generated_pages_go_unread(tmp_path):
    """Review finding 2026-11-14 (Codex): skipping every .html also skipped
    tests/fixtures/ercot_listing_12331.html, an upstream page recorded verbatim
    and published with tests/. Only the renderer's own pages are skipped."""
    from talea.markets import public_markets
    others = [m.slug for m in public_markets() if not m.primary]
    primary = [m.slug for m in public_markets() if m.primary]
    assert others and primary
    unread = ["index.html"] + [f"{s}.html" for s in others]
    read = (["tests/fixtures/ercot_listing_12331.html", "tests/fixtures/new.html",
             "docs/index.html", f"docs/{others[0]}.html"] + [f"{s}.html" for s in primary])
    files = []
    for rel in unread + read:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("<!-- landed in PR #{a} -->\n".format(**NUM))
        files.append((rel, tmp_path / rel))
    assert sorted(h.split(":", 1)[0] for h in _scan(files)) == sorted(read)


# Folders a reader's own tools create in a copy: never published.
NOT_PUBLISHED = {".git", ".venv", ".pytest_cache", "__pycache__"}


def _published_files(root: Path) -> list[tuple[str, Path]]:
    """Every file of a public copy at `root`. The mirror is published as a git
    commit and a reader's copy is a clone of it, so: what git tracks there.
    Without git (a downloaded archive), every file under `root` outside the
    folders a reader's own tools create."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, check=True).stdout.decode()
        rels = [r for r in out.split("\0") if r]
    except (OSError, subprocess.CalledProcessError):
        rels = [p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and not NOT_PUBLISHED & set(p.relative_to(root).parts)]
    return [(r, root / r) for r in sorted(rels)]


def _tree(root: Path) -> None:
    for rel in ("docs/incidents.md", "tools/new/notes.py", ".venv/lib/x.md", "a.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("text\n")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_published_files_are_what_git_tracks(tmp_path):
    _tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "docs", "tools", "a.txt"], check=True)
    assert [r for r, _ in _published_files(tmp_path)] == ["a.txt", "docs/incidents.md", "tools/new/notes.py"]


def test_published_files_without_git_skip_only_tool_folders(tmp_path):
    _tree(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "y.md").write_text("text\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "z.yml").write_text("text\n")
    rels = [r for r, _ in _published_files(tmp_path)]
    # tmp_path is not inside a git repository, so git fails and the walk is used
    assert rels == [".github/z.yml", "a.txt", "docs/incidents.md", "tools/new/notes.py"]


def test_text_skips_binary_files_only(tmp_path):
    (tmp_path / "p.html").write_text("text")
    (tmp_path / "b.ots").write_bytes(b"\x00\xff\xfe")
    (tmp_path / "s.py").write_text("text")
    assert [_text(tmp_path / n) for n in ("p.html", "b.ots", "s.py")] == ["text", None, "text"]


# Numbers put into the cases at run time, so that this file names none.
NUM = {"a": 123, "b": 45, "c": 6, "d": 7, "e": 89, "three": 3, "four": 4, "thirty_one": 31,
       "one": 1, "two": 2, "entity": 8209}


@pytest.mark.parametrize("rel, text, caught", [
    ("docs/incidents.md", "landed (PR #{a})", ["PR #{a}"]),
    ("docs/incidents.md", "see PR {a}", ["PR {a}"]),
    ("docs/incidents.md", "see pr{a}", ["pr{a}"]),
    ("docs/incidents.md", "see PR#{a}", ["PR#{a}"]),
    ("docs/incidents.md", "PRs #{b} and #{c}", ["PRs #{b}", "#{c}"]),
    ("docs/incidents.md", "PRs {b} and {c}", ["PRs {b}"]),
    ("docs/incidents.md", "since #{c}; #{d}-#{e} sat open", ["#{c}", "#{d}", "#{e}"]),
    ("docs/incidents.md", "#{d}\u2013#{e}", ["#{d}", "#{e}"]),
    ("docs/incidents.md", "draft PRs (#{b}, #{c})", ["#{b}", "#{c}"]),
    ("docs/incidents.md", "a code span `#{a}`", ["#{a}"]),
    ("docs/incidents.md", "issue#{c}", ["#{c}"]),
    ("docs/incidents.md", "&#{a};", ["#{a}"]),
    ("docs/incidents.md", "github.com/o/r/pull/{a}", ["pull/{a}"]),
    ("docs/incidents.md", "Pull/{a}", ["Pull/{a}"]),
    ("docs/incidents.md", "pull request {a}", ["pull request {a}"]),
    ("docs/incidents.md", "Pull Requests #{a}", ["Pull Requests #{a}"]),
    ("tests/test_x.py", "# landed in PR #{a}", ["PR #{a}"]),
    ("docs/ARCHITECTURE.md", "defect #{three}, is fixed", []),
    ("docs/ARCHITECTURE.md", "defect #{three} and #{four}", ["#{four}"]),
    ("docs/ARCHITECTURE.md", "defect #{four}", ["#{four}"]),
    ("docs/ARCHITECTURE.md", "Defect #{three}", ["#{three}"]),
    ("docs/ARCHITECTURE.md", "defect #{thirty_one}", ["#{thirty_one}"]),
    ("VERIFY.md", "defect #{three}", ["#{three}"]),
    ("src/talea/markets/__init__.py", "The shortlist's #{one};", []),
    ("src/talea/markets/__init__.py", "The shortlist's #{two};", ["#{two}"]),
    ("tests/test_workflow_schedules.py", "audit finding #{two}):", []),
    ("tests/test_workflow_schedules.py", "audit finding #{one}):", ["#{one}"]),
    ("scripts/render_dashboard.py", "Bitcoin&#{entity};confirmed", []),
    ("scripts/render_dashboard.py", "see #{a}", ["#{a}"]),
    ("scripts/render_dashboard.py", "(#{a})", ["#{a}"]),
    ("scripts/render_dashboard.py", "see PR #{a}", ["PR #{a}"]),
    ("scripts/render_dashboard.py", "--ink:#{a}; --g:#{b}{a}e;", ["#{a}", "#{b}{a}"]),
    ("scripts/render_dashboard.py", "see #{b}, #{e}{a}{a}, #{a}a, #{entity};", ["#{b}", "#{e}{a}{a}", "#{a}", "#{entity}"]),
    ("scripts/other.py", "Bitcoin&#{entity};", ["#{entity}"]),
    ("docs/incidents.md", "a resolver PR, then a pull request", []),
    ("docs/incidents.md", "APR {a}, sprint {c}, PR-free", []),
    ("docs/incidents.md", "the pull/ folder, # heading, {a}-{b}", []),
])
def test_pr_number_detector(rel, text, caught):
    assert [h.split(": ", 1)[1] for h in _pr_numbers(rel, text.format(**NUM))] == \
        [c.format(**NUM) for c in caught]


@pytest.mark.parametrize("colour", sorted(HTML_COLOURS))
def test_each_listed_colour_is_not_a_pr_in_the_html_writer_only(colour):
    text = f"--ink:#{colour}; a{{color:var(--x,#{colour.upper()})}}"
    assert _pr_numbers("scripts/render_dashboard.py", text) == []
    for long_name in ("--" + "a" * 200 + f":#{colour};", " --" + "a" * 200 + f":#{colour};",
                      "var(--" + "a" * 200 + f",#{colour})", "(var(--" + "a" * 200 + f",#{colour})"):
        assert _pr_numbers("scripts/render_dashboard.py", long_name) == [], long_name[:12]
    assert _pr_numbers("scripts/other.py", text) != []
    assert _pr_numbers("scripts/render_dashboard.py", f"--ink:#{colour}0;") != []
    for outside in (f"see #{colour}", f"(#{colour})", f"--ink: #{colour};", f"x=#{colour}",
                    f"# see PR:#{colour}", f"see:#{colour}", f"x,#{colour}", f"margin:#{colour};",
                    f"color:#{colour};", f"--ink:#{colour}", f"--ink:#{colour} ;", f"var(--x,#{colour}",
                    f"var(--x, #{colour})", f"x--ink:#{colour};", f"ivar(--x,#{colour})",
                    "x--" + "a" * 57 + f":#{colour};", "x--" + "a" * 200 + f":#{colour};",
                    "ivar(--" + "a" * 200 + f",#{colour})"):
        assert _pr_numbers("scripts/render_dashboard.py", outside) != [], outside


def test_listed_colours_are_all_still_used():
    """A colour no longer in the page code is taken off the list, so the
    exception never grows wider than what the file writes."""
    text = (ROOT / "scripts" / "render_dashboard.py").read_text().lower()
    unused = [c for c in sorted(HTML_COLOURS) if not re.search(rf"#{c}(?![0-9a-z_])", text)]
    assert not unused, f"HTML_COLOURS lists colours render_dashboard.py no longer uses: {unused}"


_TREE_OK = """# comment naming CLAUDE.md
  --include='/README-MOVED.md' \\
cp VERIFY.md GOVERNANCE.md pyproject.toml "$MIRROR/"
for d in a.md \\
         b.md; do
  cp "docs/$d" "$MIRROR/docs/"
done
cp README-public.md "$MIRROR/README.md"
"""


def test_named_documents_are_read_from_the_copy_commands():
    assert _named_documents(_TREE_OK) == ["README-public.md", "Data/README-MOVED.md",
                                          "Data/esios_prices.json", "VERIFY.md",
                                          "GOVERNANCE.md", "pyproject.toml", "docs/a.md", "docs/b.md"]


@pytest.mark.parametrize("change", [
    lambda t: t + 'cp NEW.md "$MIRROR/"\n',
    lambda t: t + 'cp "$x" "$MIRROR/docs/" # NEW.md\n',
    lambda t: t.replace("cp README-public.md", "cp  README-public.md"),
    lambda t: t.replace("for d in", "for doc in"),
    lambda t: t.replace("  --include='/README-MOVED.md'", "  --include='/README-NEW.md'"),
])
def test_a_document_copied_another_way_fails(change):
    with pytest.raises(AssertionError):
        _named_documents(change(_TREE_OK))


def test_open_decisions_register_is_well_formed():
    """Incident 2026-11-10: about 45 requests of the operator lived only in
    report emails and roughly half were never answered. The register is the
    tracking system; it only works if every row can be read mechanically (the
    digest counts OPEN `D-` rows and ages the oldest). Each row: an ID with a
    known prefix, an ISO date, OPEN or CLOSED, and — if CLOSED — what closed it."""
    from datetime import date
    txt = _private_text("docs/open-decisions.md")
    rows = [[c.strip() for c in l.strip().strip("|").split("|")]
            for l in txt.splitlines() if re.match(r"\|\s*[A-Z]-\d+\s*\|", l)]
    assert len(rows) >= 10, "the register has lost its rows — has the table format changed?"
    seen = set()
    for r in rows:
        assert len(r) == 5, f"register row is not 5 columns (a stray pipe?): {r[0]}"
        rid, raised, status, what, where = r
        assert re.fullmatch(r"[DTWC]-\d{2}", rid), f"bad register ID {rid!r}"
        assert rid not in seen, f"duplicate register ID {rid}"
        seen.add(rid)
        date.fromisoformat(raised)
        assert status in ("OPEN", "CLOSED"), f"{rid}: status must be OPEN or CLOSED, got {status!r}"
        assert what, f"{rid}: empty description"
        if status == "CLOSED":
            assert where, f"{rid} is CLOSED but does not say what closed it"
    assert any(r[0].startswith("D-") and r[2] == "OPEN" for r in rows) or "D-" not in txt
    # private: it names findings and people's pending decisions; never mirrored
    assert "open-decisions.md" not in (ROOT / "scripts" / "mirror_tree.sh").read_text()
    assert "open-decisions.md" not in (ROOT / "scripts" / "publish_mirror.sh").read_text()


def test_policy_carries_amendment_1_as_ratified():
    """Amendment 1 (ratified by the user in writing 2026-11-10) fixes what the
    statistical referee could not determine on 2026-11-10: the Holm family, the
    look schedule and the scope outside Spain. The referee reads the policy and
    CLAUDE.md as its pre-registration surface, so both must carry it — and the
    calibration hash clause 8 tells the referee to check must be the hash of the
    file actually in the repo, or the frozen bar has moved without a ratification."""
    import hashlib
    flat = lambda text: re.sub(r"\s+", " ", re.sub(r"(?m)^> ?", "", text))   # ignore wrapping
    pol = _private_text("docs/multiple-comparisons-policy.md")
    amend = flat(pol.split("## Amendment 1", 1)[1])
    assert "I ratify the draft as written, executable P&L deferred" in amend
    for n in range(1, 9):
        assert f"### Clause {n} — " in amend, f"Amendment 1 lost clause {n}"
    assert "0.05 / 12" in amend and "separately in each market" in amend
    assert "2026-11-11T03:30:00Z" in amend, "the ratification instant defines 'forward only'"
    digest = hashlib.sha256((ROOT / "Data" / "calibration" / "sign_bar.json").read_bytes()).hexdigest()
    assert digest in amend, ("Data/calibration/sign_bar.json no longer matches the hash recorded "
                             "in Amendment 1 — changing the frozen calibration is user-gated")
    claude = flat(_private_text("CLAUDE.md"))
    assert "Amendment 1" in claude and "0.05/12" in claude and "2026-11-11T03:30:00Z" in claude
    prop = _private_text("docs/proposals/comparison-rules-amendment-2026-11.md")
    assert "STATUS: RATIFIED" in prop and "NOT IN FORCE" not in prop
