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
