"""Documentation-drift detectors (closed defect loop: doc staleness is a
failure class, so it gets a regression test). These FAIL the build when a
market or strategy is added in code but not named in the docs — which is
exactly how ARCHITECTURE.md fell behind the multi-market build.

Deliberately cheap: they check that each code-registered name APPEARS in the
canonical doc, not that prose is worded a particular way. Live numbers are NOT
checked here — those belong in the ledger/status/dashboard, never frozen in a
doc."""
import re
from pathlib import Path

from talea.loop import STRATEGIES
from talea.markets import MARKETS

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
CLAUDE = (ROOT / "CLAUDE.md").read_text()
INFRA = (ROOT / "INFRA.md").read_text()


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
    missing_claude = [n for n in names if n not in CLAUDE]
    assert not missing_arch, (
        f"strategies in code but not in docs/ARCHITECTURE.md panel: {missing_arch}")
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
    html = (ROOT / "docs" / "system-map.html").read_text()
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
    routines = ROOT / "docs" / "routines"
    mirrors = [p for p in sorted(routines.glob("*.md"))
               if "## Prompt (verbatim)" in p.read_text()]
    assert len(mirrors) >= 5, "expected the standing routine mirrors to be present"
    infra = INFRA
    readme = (routines / "README.md").read_text()
    for p in mirrors:
        assert p.stem in infra, (
            f"routine mirror {p.stem!r} is not named in INFRA.md's cloud-routines "
            f"map — update the MIRRORS list + the count when you add/remove a routine")
        assert p.stem in readme, (
            f"routine mirror {p.stem!r} is not named in docs/routines/README.md")
    smap = (ROOT / "docs" / "system-map.html").read_text()
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
    rows = _alert_channel_rows(INFRA)
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
    txt = (ROOT / "scripts" / "publish_mirror.sh").read_text()
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
    for rel in ("mirror/verify.yml", "README-public.md", "VERIFY.md",
                "GOVERNANCE.md", "DATA-SOURCES.md", "docs/ARCHITECTURE.md"):
        txt = (ROOT / rel).read_text()
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
    readme = (ROOT / "README-public.md").read_text()
    footer = (ROOT / "scripts" / "render_dashboard.py").read_text()
    for name, text in (("VERIFY.md", verify), ("README-public.md", readme),
                       ("render_dashboard.py", footer)):
        assert "dispatch order" in text, f"{name} lost the dispatch-order disclosure"
        for bad in ("relative metrics are robust.", "are robust to that. Hourly",
                    "are robust to this."):
            assert bad not in text, f"{name} again claims relative metrics are robust, unqualified"
    assert "scripts/feasibility_report.py" in verify and "NOT robust" in verify
    assert (ROOT / "scripts" / "feasibility_report.py").exists()


def test_open_decisions_register_is_well_formed():
    """Incident 2026-11-10: about 45 requests of the operator lived only in
    report emails and roughly half were never answered. The register is the
    tracking system; it only works if every row can be read mechanically (the
    digest counts OPEN `D-` rows and ages the oldest). Each row: an ID with a
    known prefix, an ISO date, OPEN or CLOSED, and — if CLOSED — what closed it."""
    from datetime import date
    txt = (ROOT / "docs" / "open-decisions.md").read_text()
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
    pol = (ROOT / "docs" / "multiple-comparisons-policy.md").read_text()
    amend = flat(pol.split("## Amendment 1", 1)[1])
    assert "I ratify the draft as written, executable P&L deferred" in amend
    for n in range(1, 9):
        assert f"### Clause {n} — " in amend, f"Amendment 1 lost clause {n}"
    assert "0.05 / 12" in amend and "separately in each market" in amend
    assert "2026-11-11T03:30:00Z" in amend, "the ratification instant defines 'forward only'"
    digest = hashlib.sha256((ROOT / "Data" / "calibration" / "sign_bar.json").read_bytes()).hexdigest()
    assert digest in amend, ("Data/calibration/sign_bar.json no longer matches the hash recorded "
                             "in Amendment 1 — changing the frozen calibration is user-gated")
    claude = flat((ROOT / "CLAUDE.md").read_text())
    assert "Amendment 1" in claude and "0.05/12" in claude and "2026-11-11T03:30:00Z" in claude
    prop = (ROOT / "docs" / "proposals" / "comparison-rules-amendment-2026-11.md").read_text()
    assert "STATUS: RATIFIED" in prop and "NOT IN FORCE" not in prop
