# Architecture

What this application does: every day, BEFORE a day-ahead auction publishes
tomorrow's electricity prices, it commits immutable "receipts" recording how a
panel of pre-registered strategies would trade a virtual 1 MW / 2 MWh battery;
after publication it settles those receipts against the real prices, appends
money-denominated results to an append-only ledger, anchors everything in
Bitcoin via OpenTimestamps, and republishes a public, auditable dashboard. It
is a measurement instrument: the product is a track record that cannot be
faked, revised, or cherry-picked. Spain is the primary market; the same loop
now runs eight markets (see "Markets" below). Governance (who may change what,
and how claims are verified) is in CLAUDE.md; outsider verification in
VERIFY.md. The cross-disciplinary domains this project spans (and where each
lives) are mapped in docs/knowledge-map.md.

## Markets (updated 2026-08-27)

The loop is market-agnostic (a frozen `Market` dataclass parameterizes zone,
timezone, commit deadline, currency, Data/<slug>/ tree, and fetch client — the
strategy panel, P&L math, and guards are shared). Eight markets run today:

    market  zone      source / license               cur  deadline  writer        public tier
    ES      Spain     apidatos.ree.es · public        EUR  13:00     server        Pages + Bitcoin-OTS
    DE      DE-LU     SMARD.de · CC BY 4.0            EUR  12:00     server        Pages + OTS
    IT      IT-SUD    ENTSO-E A44 · token, derived    EUR  12:00     server        private (license)
    PT      PT        ENTSO-E A44 · token, derived    EUR  12:00     server        private (license)
    FR      France    ENTSO-E A44 · token, derived    EUR  12:00     server        private (license)
    GB      GB        Elexon BMRS Market Index · open  GBP  11:00     server        Pages + OTS (within-day index feed, see note)
    JP      JEPX      JEPX spot · open (attribution)  JPY  10:00     GitHub Actions private (silent-first; redistributable)
    ERCOT   HB_NORTH  ERCOT MIS NP4-190 · public/redist USD 10:00    GitHub Actions Pages + OTS

GB note (audit finding 2026-09-07): GB's feed is the Elexon BMRS Market Index
(APXMIDP leg) — a traded within-day reference index that populates
progressively through the delivery day, NOT the GB day-ahead auction clearing
price. Receipts are still leak-guarded (no price for the target day exists at
the 09:00 UTC tick), but yesterday's profile is never complete at that tick, so
the persistence primary (and rank-blend, which needs it) structurally never
commit in GB; only climatology and weekly do. Disclosed on gb.html
(`presentation.note`) and in VERIFY.md; whether to move GB to a genuine
day-ahead source is a user decision (escalated 2026-09-07).

Two writers, not one: the server writes ES/DE/IT/PT/FR/GB; ERCOT and JP are driven from
GitHub Actions runners (ERCOT DAM geo-blocks EU IPs; JEPX publishes ~10:10 JST,
the European night). Both push directly to
`main`; the `main-protection` ruleset blocks force-push and branch deletion so
the append-only trail cannot be rewritten. IT/PT/FR are private because ENTSO-E
day-ahead is not freely redistributable (derived-metrics-only if ever
published); ES, DE, GB, and ERCOT are PUBLIC — all four on ONE Talea mirror repo (one project, each market a first-class page + Data/<slug>/ under it; render_dashboard.py --site, registry-derived deny-by-default allowlist). JP stays private (silent-first) pending its JEPX-licence confirmation. Historical
cross-market capture is in docs/backtest-markets-2026-08.md.

The `markets/` package is the SINGLE SOURCE OF TRUTH (Phase 0, 2026-08-27; became
a package in Phase 1, 2026-08-28): `markets/__init__.py` holds the registry and
each `Market` carries flags (`primary`/`public`/`driver`/`redistributable` +
`presentation`), and every operational surface — the email digest, `server_tick.sh`
(via `python -m talea markets --driver server`), `render_dashboard.py`, this
table — QUERIES the registry instead of hardcoding a slug subset. The market
CONTRACT (the `Fetcher` protocol + the re-exported `Market`/`Presentation` types,
which stay defined in the `loop.py` core) lives in `markets/base.py`. Drift is
caught by `test_market_registry.py` + `test_market_conformance.py` (every market
has its guards) + `test_docs_in_sync` (this table must match the registry's flags).
Per-market VERTICAL modules (`markets/<slug>/`): **all seven markets are vertical
modules now** (Phase 2 code refactor complete), and **ES data lives at `Data/es/`
like every other market** (Stage B, 2026-08-28) — `Data/` root holds only
shared/project artifacts (`esios_prices.json` deep history, `calibration/`) plus one
subdir per market; no market is privileged. `loop.py` no longer imports a market's
fetcher at
module scope (the ES fetcher is imported lazily inside `_default_market`), so the
core is free of a module-time market dependency. **A `markets/<slug>/fetch.py`
exists to house a
market's DEDICATED fetch LOGIC** — DE owns the SMARD client, ERCOT the MIS pipeline
(`talea.fetch_smard`/`fetch_ercot` are compat shims). Markets that use the
SHARED ENTSO-E A44 library (`markets/_entsoe.py`) own no fetch logic, so they have
no `fetch.py`: their fetch is a one-line EIC/tz binding inline with the Market
config. **IT, PT, and FR are INDEPENDENT markets** — each owns its EIC + timezone
and binds the library; `markets/_entsoe.py` holds NO market constants, so a change
to one cannot touch another (the old `fetch_entsoe.py` shared-module entanglement,
defect #3, is fixed). `talea.fetch_entsoe` is a compat shim re-exporting the
library. See docs/market-plugin-refactor-plan.md.

## Components

    ┌──────────────── Hetzner VPS (primary writer; ERCOT via GitHub Actions) ────────┐
    │ systemd timers                                                                │
    │  esios-tick.timer      11:00 / 12:30 / 17:00 Europe/Madrid → server_tick.sh   │
    │  esios-ots-upgrade     Sun 12:00 Madrid → weekly_maintenance.sh               │
    │                                                                               │
    │ server_tick.sh: git pull → python -m talea tick → publish_mirror.sh     │
    └──────────────┬──────────────────────────────────────────────┬─────────────────┘
                   │ git push (audit trail)                       │ git push (allowlist)
                   ▼                                              ▼
        GitHub private repo                          GitHub public mirror ── Pages
        (code + Data/)                               (Data/, src/, VERIFY, dashboard)
                   ▲                                              │
                   │ analysis clone, sessions                     ▼ raw reads (no auth)
              Mac (cockpit)                          Cloud routines (claude.ai):
                                                      - daily digest 16:35 UTC
                                                      - Monday independent auditor

    External: apidatos.ree.es (price route A) · api.esios.ree.es (route B,
    weekly cross-check) · OTS calendar servers → Bitcoin · healthchecks.io
    (dead-man) · Gmail SMTP 587 (inbox digest)

## Package layout

    src/talea/     (map: src/talea/README.md)
      loop.py      THE FUNCTIONAL CORE. Tick orchestrator, leak/clock guards,
                   settlement + P&L math, storage, telemetry. Market-agnostic.
      __main__.py  THE IMPERATIVE SHELL. CLI, git backup, OTS stamping, heartbeat,
                   email digest. Injection seams (_smtp, _urlopen, _run) for tests.
      markets/     THE MARKET PLUGINS. __init__ = the registry (single source of
                   truth); base.py = the contract; _entsoe.py = shared A44 library;
                   es/de/ercot own a fetch.py (dedicated client), it/pt/fr bind
                   _entsoe inline. (Compat shims deleted in Phase 4, 2026-08-28.)
    tools/esios-fetcher/   route-B client (token, PriceDay contract, own tests)
    scripts/               server_tick, weekly_maintenance, publish_mirror,
                           render_dashboard, backtest, compare, crosscheck
    tests/                 failure-mode-first (see "Test suite" below)
    Data/                  the product (see Data flow)

## Call flow of one tick (loop.tick, via __main__.cmd_tick)

    1. today = market_today()            # Europe/Madrid date, machine-TZ-proof
    2. FETCH  fetch_hourly(last_known → tomorrow)
              retries +5m/+15m; validate_prices rails (-500..4000, finite);
              a failing/insane fetch NEVER touches the dataset (stale is safe)
    3. SETTLE for every receipt whose target day is now fully published and
              not yet in the ledger (keyed target+strategy+version):
                pnl_eur(buy, sell, actual)      # 0.85 RT on sell leg, 0.5 fees
                oracle = pnl_eur(pick_hours(actual))   # perfect hindsight
                capture = pnl/oracle · tau = kendall_tau(basis_profile, actual)
                + regime telemetry (neg_hours, tb2_spread)  → append ledger
    4. COMMIT (tomorrow's receipts) — THE LEAK GUARD:
                if any price for tomorrow exists in OUR dataset → refuse all
                else for each STRATEGIES entry: basis_fn(prices, today) →
                  (basis|None, why); pick_hours(basis) → receipt appended
                  (idempotent per target+strategy; includes basis_profile)
    5. __main__ shell, in order (each best-effort, never fatal):
                OTS stamp (manifest of receipts+ledger hashes) → git commit+
                push Data/ → heartbeat (ping iff primary receipt stands;
                /fail otherwise) → email digest (only on settle or trouble)
    6. server_tick.sh: render_dashboard → publish_mirror (allowlist → Pages)

## Data flow and formats

    apidatos ──fetch──▶ Data/es/prices.json          [{"ts","price"}] hourly,
                                                  dataset of record; leak guard
                                                  is defined against THIS file
    strategies ─commit─▶ Data/es/receipts.jsonl      append-only; target, hours,
                                                  basis_day, basis_profile,
                                                  params, committed_at (UTC)
    publication ─settle─▶ Data/es/ledger.jsonl       append-only; pnl_eur, oracle,
                                                  capture, tau, neg_hours,
                                                  tb2_spread, settled_at
    both ──sha256──▶ Data/es/ots/<date>.txt(.ots)    daily manifest + OTS proof;
                                                  Bitcoin-anchored weekly
    route B (weekly) ─▶ Data/esios_prices.json    independent deep history
                                                  (2015→), cross-checked vs A;
                                                  disagreement → CROSSCHECK-
                                                  ALERTS.log (committed, loud)
    everything ──▶ git ──▶ mirror ──▶ Pages dashboard + routines + auditors

## Strategy panel (registry in loop.STRATEGIES)

    persistence v1 (PRIMARY)  basis = yesterday's profile
    climatology v1 (shadow)   basis = per-hour mean of trailing 28 complete days
    rankblend  v1 (shadow)    basis = mean of the two legs' per-hour ranks
    weekly     v1 (shadow)    basis = same hour last week, p(d-7,h) — the EPF
                              literature's canonical naive (registered 08-22)
    Adding one = one registry entry + basis_fn. Promotion/retirement are
    mechanical (CLAUDE.md rules R1/R7). Settlement math never changes per
    strategy — comparability is the point. Live per-strategy P&L/capture is not
    frozen here — it lives in the ledger, `... status`, and the dashboards.

## Failure paths (designed, tested)

    fetch fails/insane   → retry, then stale-data tick; commitment may still
                           be legal (guard checks OUR dataset); honest miss
                           otherwise. NEVER a corrupted dataset.
    late tick            → leak guard refuses commit; missed day stands forever
    shell step fails     → logged, tick continues; missing heartbeat/email IS
                           the alarm (dead-man design)
    server dies          → Persistent=true catch-up on boot; monitor chain in
                           docs; restore drill proven from GitHub alone
    silent corruption    → weekly two-route cross-check + Monday auditor
                           recomputing everything from public data

## Automation & verification (updated 2026-08-27)

    Cloud routines (claude.ai, Gmail/push-notified):
      daily digest        ~16:35 UTC — ledger email
      Monday auditor      07:00 UTC  — fresh-context, READ-ONLY (never writes)
      Monday resolver     09:00 UTC  — fixes audit findings, auto-merges GREEN
                                       PRs (CI-gated), escalates gated items
      month-end review    1st 08:00 UTC
      gate reminder       one-time 2026-09-04 — GBM v2 override window closes
    CI (GitHub Actions, on push/PR):
      pytest under a COVERAGE GATE  +  verify_ledger.py --all (re-derives EVERY
      market's ledger; a discrepancy fails the build). See "Test suite" below.
    Branch protection:  main-protection ruleset — force-push + deletion blocked.
    Independent re-derivation (anyone can run it):
      scripts/verify_ledger.py --all [--verify-ots]  — recomputes every
      settlement from raw prices + each receipt's own params (imports nothing
      from the package), re-checks the leak guard, proves append-only via the
      OTS-manifest prefix hashes, reports Bitcoin coverage. Tamper-tested
      (tests/test_verify_ledger.py). It runs in CI, and the ONE public `talea`
      mirror runs it as its OWN workflow (`verify.yml` → `verify_ledger.py --all`
      over every public market) → the single green "verify" badge in the talea
      README is a live, anonymous check across ES/DE/GB/ERCOT, not decoration.
      (Consolidated 2026-08-29 from the former per-mirror workflows.) Private
      markets (IT/PT/FR/JP) are covered by the private CI's `--all`, not published.

## Test suite & coverage gate (updated 2026-08-27)

Philosophy: FAILURE-MODE-FIRST. A test earns its place by failing the way a
real bug (or bad actor) would — the leak guard refusing a late commit, a
doctored P&L, a rewritten OTS manifest, a swallowed push error — not by
confirming the happy path. 162 tests; `uv run pytest`.

    Levels covered:
      unit         pure functions — pnl_eur, pick_hours, kendall_tau,
                   validate_prices, the sign-test binomial
      integration  tick orchestration (commit→settle), multi-market isolation,
                   cmd_tick/status/main dispatch, the digest, git_backup
      contract     each fetcher's fetch_hourly replayed against a REAL recorded
                   response (tests/fixtures/, captured 2026-08-20/27): ES
                   apidatos ¼h→hourly, DE SMARD bucket, PT ENTSO-E A44, ERCOT
                   MIS listing→zip→CSV. Pins the upstream shape a refactor could
                   silently break; live drift is a separate smoke-test concern.
      end-to-end   cmd_tick full pass through injection seams (_smtp/_urlopen/
                   _run) — fetch→settle→commit→OTS→push→heartbeat→email

    Coverage GATE (CI, --cov-fail-under, RATCHET: raise never lower):
      floor 98% (2026-08-27) over the core package + every production-critical
      script — loop.py (money/guards) ~99%, render_dashboard (public numbers),
      verify_ledger (independent re-derivation), compare_strategies (R1
      promotion test), crosscheck_routes (corruption guard), audit_ots_manifests
      (OTS immutability). Config: pyproject [tool.coverage.run].
      OMITTED (analysis-only, human-run, low blast radius): backtest_baselines,
      backtest_markets, probe_signal.

Deeper dives: VERIFY.md (outsider audit), docs/gate-analysis-plan.md
(evaluation), docs/evolution-plan.md (how it advances), docs/incidents.md
(what has actually broken), docs/BACKLOG.md (what's next).
