# Talea — knowledge map

What kinds of knowledge this project actually requires, and where each lives in
the repo. Talea is a paper-trading loop on day-ahead electricity prices, but its
real subject is **measurement**: producing a committed-before-truth,
tamper-evident public track record. That goal pulls in an unusually wide set of
domains. This map is the newcomer's index to them.

The through-line: domains 1–3 define **what** is measured, 4–7 make the
measurement **trustworthy and durable**, and 8–10 govern **who may act on it and
why it is worth anything**. The distinctive bet is not any single domain — it is
that a *committed-before-truth, tamper-evident record* is the scarce asset, which
is why cryptographic attestation sits at the centre rather than the forecasting.

---

## 1. Power markets & electricity economics
Day-ahead auction mechanics across markets (Spain OMIE/ESIOS, Germany SMARD,
GB Elexon, ERCOT, ENTSO-E IT/PT/FR, Japan JEPX): publication timing, price
formation, negative prices, solar-heavy midday collapse, regime shifts, the
15-min MTU transition. Battery/BESS arbitrage: 2-cheap / 2-dear hour-picking,
round-trip efficiency, explicit fees, **capture vs the perfect-hindsight oracle**.
- Where: `src/talea/loop.py` (P&L + oracle math), `src/talea/markets/<slug>/`
  (per-market zone/deadline/currency), `docs/research-landscape-2026-08.md`,
  `docs/backtest-markets-2026-08.md`.

## 2. Electricity price forecasting (EPF)
Naive baselines (persistence, climatology, rank-blend, weekly), the Lago/Weron
benchmark literature, hour-**ranking** vs level forecasting, Kendall tau-b as the
arbitrage-value metric, and the GBM/LightGBM escalation gate (lags + calendar +
weather covariates that must beat persistence out-of-sample).
- Where: `loop.STRATEGIES`, `docs/gate-analysis-plan.md`,
  `docs/gate-verdict-2026-08.md`, `docs/backtest-baselines-2015-2026.md`.

## 3. Statistics & experimental methodology
Pre-registration, falsification criteria, signal gating (day-shuffled nulls, ACF,
σ-tests), the sign-test comparison bar, moving-block-bootstrap calibration,
multiple-comparisons control (Holm; Option C `p_eff = max(p_iid, p_boot)`),
temporal splits, and an independent statistical referee that re-derives any claim
before publication.
- Where: `scripts/compare_strategies.py`, `scripts/calibrate_sign_bar.py`,
  `scripts/probe_signal.py`, `docs/multiple-comparisons-policy.md`,
  `docs/probe-signal-reproduction-2026-08.md`, CLAUDE.md § Falsification / Deciding
  strategy comparisons.

## 4. Cryptographic attestation & tamper-evidence
Committed-before-truth (the leak guard), append-only audit trails, SHA-256
manifests, **OpenTimestamps → Bitcoin anchoring**, the exact semantics of what a
timestamp does and does not prove, and per-market evidence-boundary disclosure.
- Where: the leak/clock guards in `loop.py`; OTS stamping in `__main__.py` +
  `scripts/weekly_maintenance.sh`; `scripts/audit_ots_manifests.py`;
  `Data/<slug>/ots/`; `VERIFY.md`; `GOVERNANCE.md`.

## 5. Software engineering
Functional-core / imperative-shell split, stdlib-only discipline, the market
plugin/registry pattern, failure-mode-first + property-based tests, a CI coverage
gate, idempotency, and durability (fsync, atomic `os.replace`, fcntl write lock).
- Where: `src/talea/loop.py` (core) vs `__main__.py` (shell); `src/talea/markets/`
  (registry = single source of truth); `tests/`; `.github/workflows/ci.yml`;
  `src/talea/README.md`; `docs/ARCHITECTURE.md`; `docs/market-plugin-refactor-plan.md`.

## 6. Infrastructure & DevOps
Hetzner VPS + systemd timers, GitHub Actions (geo-egress routing for ERCOT/JP),
GitHub Pages, deploy keys, dead-man switches, SMTP + ntfy alerting, secrets under
a permanent no-sudo constraint, and disaster-recovery runbooks.
- Where: `scripts/server_tick.sh`, `scripts/publish_mirror.sh`,
  `.github/workflows/ercot.yml` + `jp.yml`, `mirror/verify.yml`.

## 7. Data engineering
Multi-source fetching (REST, tokened APIs, CSV), timezone/DST normalization,
quarter-hourly → hourly aggregation, input-validation rails (a bad feed never
touches the dataset), weekly cross-route reconciliation, and dataset provenance.
- Where: `src/talea/markets/<slug>/fetch.py` + `markets/_entsoe.py`;
  `tools/esios-fetcher/`; `scripts/crosscheck_routes.py`;
  `Data/<slug>/prices.json`; `Data/esios_prices.json` (frozen deep history).

## 8. Data licensing, IP & legal
Per-source redistribution terms (REE public, SMARD CC BY 4.0, Elexon open, ERCOT
NP4-190 public, JEPX attribution, ENTSO-E **non**-redistributable), attribution,
deny-by-default publishing, code licensing (MIT), and naming/trademark diligence.
- Where: `DATA-SOURCES.md`, `Data/<slug>/LICENSE.md`, `LICENSE` (code),
  the allowlist in `scripts/mirror_tree.sh` (what `publish_mirror.sh` publishes).

## 9. AI-agent governance & orchestration
The audit → resolve cycle (independent read-only auditor + a resolver that fixes
under a mandate), the closed defect loop, the standing mandate & autonomy classes,
escalation vs auto-merge, and persistent file memory — the machinery that lets the
project run and correct itself.
- Where: CLAUDE.md § Standing mandate / audit-resolve cycle / closed defect loop;
  `docs/incidents.md` (escape-rate ledger); `GOVERNANCE.md`.

## 10. Why the record is the asset
The scarce, hard-to-fake thing here is the **track record itself** — a
committed-before-truth, tamper-evident, independently-verifiable ledger — not the
day-ahead hour-picking, which many can do. That is why the whole system is
engineered around attestation and independent verifiability rather than forecast
accuracy: the measurement's *credibility* is the point.
- Where: `README.md`, `VERIFY.md`, `docs/research-track-records-2026-08.md`.

---

*Living document — add a domain here when the project takes on a genuinely new
kind of knowledge, not merely a new instance of an existing one.*
