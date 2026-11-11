# How to verify Talea (for a skeptic)

The claim, for EVERY market under `Data/<slug>/` (es, de, gb, ercot): every
trading decision in `Data/<slug>/receipts.jsonl` was committed BEFORE the prices
it would be judged against were published, and every P&L figure in
`Data/<slug>/ledger.jsonl` follows mechanically from those receipts and publicly
available prices. You should not have to trust us on any of it.

The walk-through below uses **Spain (es)** — the longest-running market with the
strongest attestation — as the worked example; substitute any `<slug>` and the
same commands apply. The arithmetic checker in section 2 already covers every
market at once (`--all`).

## Markets and their attestation tier

Not every market carries the same strength of evidence yet; stated plainly:

- **es** — OTS-anchored (Bitcoin) from 2026-08-08; git-corroborated from
  2026-07-30. The full three-tier story is in section 1.
- **de** — OTS-stamped from 2026-08-27; the older proofs are Bitcoin-CONFIRMED
  and only the newest (1-2 days old) are still "pending" until the weekly
  `ots upgrade` matures them — check any date's actual state with `ots info
  Data/de/ots/<date>.txt.ots` rather than trusting this sentence; the earlier
  days (2026-08-24 → 08-26) are git-attested only. **Evidence boundary (parallel to
  ES's above):** DE ran first on its own public mirror (`germany-dayahead-ledger`,
  now frozen) and in the private code repo; DE's history entered THIS consolidated
  `talea` repo as a single bulk sync at the 2026-08-29 one-project consolidation.
  So the timing "weak check" (comparing a receipt's `committed_at` to the push
  time of the commit that introduced it) does NOT corroborate pre-2026-08-29 DE
  dates *from this repo alone* — use the OTS proofs (08-27+), or the per-tick
  history in the private repo / the frozen `germany-dayahead-ledger` mirror (shown
  on request). Arithmetic and append-only hold regardless.
- **ercot** — launched 2026-08-27; OTS-stamped from its first tick, and the
  proofs are Bitcoin-CONFIRMED as they mature (the newest may still be
  "pending" for 1-2 days — `ots info` on the file is the ground truth).
  **Evidence boundary** (same shape as DE's above): ERCOT's history entered
  THIS consolidated `talea` repo in the same 2026-08-29
  one-project-consolidation commit (`fd4bdd49`) that bulk-imported DE —
  `git log` shows no earlier commit touching `Data/ercot/`. So the timing
  "weak check" does NOT corroborate ERCOT's 2026-08-27/28 `committed_at`
  values *from this repo alone* either;
  rely on the OTS manifests or the GitHub Actions run history for the
  `ercot.yml` workflow (shown on request). Arithmetic and append-only hold
  regardless.
  **Second evidence boundary — targets 2026-11-05 → 2026-11-11** (server-sync
  outage; `docs/incidents.md`, 2026-11-09 row): ERCOT ticks on GitHub Actions
  and reaches this repo only through the publishing server, whose private
  clone could not pull for a week. So ERCOT's receipts for those seven targets
  entered this repo together in ONE commit (`7f05f41`, 2026-11-10T23:03:58Z) —
  AFTER the prices for every one of them had published — and the ERCOT page
  sat frozen at target 2026-11-04 throughout. The "weak check" therefore does
  NOT corroborate those seven targets from this repo. What does: the OTS
  manifests `Data/ercot/ots/2026-11-04*` → `2026-11-10*`, each stamped by the
  Actions run at tick time (still "pending" until the weekly upgrade matures
  them — `ots info` is the ground truth), and the `ercot.yml` run history plus
  the on-time per-tick commits in the private repo (shown on request). The
  receipts themselves were not written late: every `committed_at` in the
  window is 7.7–9.1 hours ahead of its 13:30 CT publication.
- **gb** — launched 2026-08-28; committed receipts per-tick from 2026-08-30,
  settled days from target 2026-08-31 (shadow strategies — see below), OTS-
  stamped manifests from 2026-08-29 (the earliest Bitcoin-CONFIRMED, the newest
  still "pending" — same maturation lag as DE/ERCOT). GB entered this repo via
  its own per-tick commits, not a bulk import, so (unlike DE/ERCOT) the "weak
  check" corroborates its receipts from this repository alone with no
  evidence-boundary caveat. Every settled GB row re-derives from
  `Data/gb/prices.json` with `verify_ledger.py --all` like any other market.
  **Two GB-specific disclosures (independent audit, 2026-09-07):**
  (1) *The feed is not a day-ahead auction.* GB settles against the Elexon BMRS
  Market Index (APXMIDP leg): a traded within-day reference index that
  populates progressively through the delivery day itself (the git history of
  `Data/gb/prices.json` shows each tick's last stored hour ~1h before the
  tick), not the GB day-ahead auction clearing price (EPEX/N2EX, licence-
  restricted). The commit-before-truth claim still holds — at the 09:00 UTC
  tick no price for the target day exists, so the leak guard is satisfied and
  the receipt predates every price it is judged against — but "day-ahead" here
  means "committed the day before, settled on an ex-post traded index", which
  a skeptic should weigh differently from ES/DE/ERCOT's auction clearing prices.
  (2) *The primary strategy never commits in GB, by construction.* Persistence
  needs yesterday's COMPLETE 24-hour profile at the pre-deadline tick; under
  this feed today's hours are still arriving, so `battery-2h2h-persistence`
  (and `rankblend`, which needs it) have ZERO GB receipts — only climatology
  and weekly commit. This is a structural property of the feed, not an outage
  or a missed day, and it is why the GB page shows a shadow-only panel with no
  primary total. Whether GB moves to a genuine day-ahead source is an open
  user decision; until then this bullet and the page's Disclosure banner
  state the situation as it is.

## 1. The timing claim (receipts predate the auction)

The Spanish day-ahead auction publishes prices for delivery day T at ~13:15
CET on T-1. A receipt for target T is honest only if it existed before that.

**Weak check (trusts GitHub):** every tick commits and pushes `Data/`. Compare
any receipt's `committed_at` (UTC, inside the file) with the push time of the
commit that introduced it: `git log --follow --format='%H %cI' -- Data/es/receipts.jsonl`.

**Strong check (trusts no one):** `Data/es/ots/<date>.txt` holds the SHA-256 of
both audit files at stamp time; `<date>.txt.ots` is an OpenTimestamps proof
anchoring that manifest in Bitcoin. Verify with the open-source client:

    sha256sum Data/es/receipts.jsonl        # compare against the manifest text
    ots verify Data/es/ots/<date>.txt.ots   # proves the manifest existed at time X

(Proofs are stamped daily and upgraded to Bitcoin-anchored weekly; a
just-created proof may still be "pending" — re-run `ots upgrade` later.)

**Evidence boundaries, stated plainly.** This public mirror was created on
2026-08-10: days before that date entered it in one bulk commit, so the
"weak check" above cannot corroborate them from this repository alone (the
private operations repository holds their per-tick commit history, shown on
request). Precisely (independent audit, 2026-09-07): the mirror's first
commit landed at ~22:59 UTC on 2026-08-10 — AFTER that day's 13:15 CET
publication — so the receipt for target 2026-08-11 (committed the morning of
08-10) is not weak-check-corroborated either; per-tick corroboration from this
repository starts at target 2026-08-12. The 08-11 receipt is covered by the
strong check (its 2026-08-10 manifest is Bitcoin-confirmed). OTS attestation
begins 2026-08-08; the first week (2026-07-30 →
2026-08-07) has no Bitcoin anchoring and cannot acquire it retroactively —
timestamps cannot be backdated, which is the entire point of the mechanism.
Treat the record as three tiers of strength: OTS-anchored (2026-08-08+),
git-corroborated (private repo, 2026-07-30+), and the mirror's own history
(2026-08-10+). On 2026-08-21 an independent audit found that same-day
re-ticks had been silently rewriting stamped manifests, invalidating 12
proofs; the stamped originals were restored from git history, and the tick
was fixed so stamped manifests are immutable (changed state gets a new
suffixed manifest), with a regression test pinning the class. A follow-up
2026-08-24 audit found that restoration correct-but-incomplete: for 13
dates (2026-08-09 through 2026-08-21) the OTS-stamped manifest anchors the
FIRST daily tick's file state, not that day's final settlement — the
displaced final state was preserved (`<date>-2.txt`) but never itself
submitted to OpenTimestamps, so those 13 dates currently have no Bitcoin
anchor of their final content, only of an earlier, partial one. This does
not affect any P&L figure (all settlement arithmetic re-verifies exactly
against Data/es/prices.json); it narrows what the "strong check" proves for
those 13 dates specifically. From 2026-08-22 on, every tick's manifest
slot is stamped, closing the gap going forward
(`scripts/audit_ots_manifests.py` now detects any recurrence).
Why this gap is evidentially immaterial (and why we do NOT mint late
proofs to paper over it): the only claim that requires tamper-evident
*pre-dating* is that RECEIPTS predate publication — and every receipt is
anchored, because each day's receipt is committed at that day's first
(morning) tick, whose manifest is exactly the anchored one. SETTLEMENTS are
deterministic recomputations of P&L from the anchored receipt plus public
prices; their integrity rests on public recomputability and the append-only
git history, not on a timestamp (a timestamp proves "existed before X",
which is meaningful for a pre-publication commitment and vacuous for a
post-publication settlement). Minting a proof dated today for an August-9
settled state would be honest but would prove only "existed by today" —
adding no pre-publication evidence — so we don't. **STATUS (ratified
2026-08-29): this is a SETTLED, permanent, disclosed limitation, NOT an open
action item** — like the pre-2026-08-08 no-OTS window above, it will not be
"resolved" because minting adds nothing a skeptic should value; it is stated
here so the record is complete.
**Server-sync outage, 2026-11-04 → 2026-11-10** (`docs/incidents.md`,
2026-11-09 row). ES, DE and GB kept reaching this mirror at every tick, so
their weak check holds through the window, with one caveat: `%cI` is the time
a commit was MADE, which normally precedes the push by seconds. The publish
of 2026-11-04 10:01 UTC failed to push and reached GitHub with the next one
at 11:31 UTC — 90 minutes later than its `%cI` says, though still ahead of
that day's ES (12:15 UTC) and DE (11:40 UTC) publications, and GB's index
for a target day does not begin to populate until the day itself. (A second failed
push, 2026-11-05 16:01 UTC, carried settlements but no receipts.) ERCOT was
hit harder — see its bullet above. In the PRIVATE repo the server's ticks for
the whole window were pushed late in one batch on 2026-11-10: their author
dates are original, their committer dates are the recovery time, so for
those days this mirror's history and the OTS proofs — not the private repo's
commit times — are the corroboration.
Data/esios_prices.json (the independent cross-check route) updates
WEEKLY by design — staleness under 7 days is normal.

## 2. The arithmetic claim (P&L follows from receipts + public prices)

Prices are public: apidatos.ree.es (no key) or api.esios.ree.es (free key),
both derived from the OMIE auction. For any settled day:

    pnl = sum(price[h] for h in sell_hours) * 1.0 MW * 0.85
        - sum(price[h] for h in buy_hours) * 1.0 MW
        - 0.5 EUR/MWh * (2 * 1.0 + 2 * 0.85)        # fees on energy moved

The oracle is the same formula over the day's 2 cheapest / 2 dearest hours;
capture = pnl / oracle_pnl. The exact code is `src/talea/loop.py`
(pure functions, unit-tested: `uv run pytest`). The independent second price
route is cross-checked weekly (`scripts/crosscheck_routes.py`).

**One command that does all of the above for you:**

    uv run python scripts/verify_ledger.py --all [--verify-ots]

`scripts/verify_ledger.py` re-derives EVERY settlement from `prices.json` and
each receipt's own recorded params — from scratch, importing nothing from the
project's own code, so a bug in our P&L path cannot hide in the checker — and
diffs it against `ledger.jsonl` line by line. It also re-checks the leak guard
(every receipt's `committed_at` predates its target day), the append-only
property (each anchored OTS manifest's SHA-256 must match a prefix of the
current file — a rewritten history matches nothing and FAILs), and reports how
many receipts are OTS-covered (a stamped manifest exists for them; whether that
proof is still *pending* or already Bitcoin-confirmed is only checked with
`--verify-ots`, which shells `ots verify` per proof). Exit code 0 = every check passed. It ships
with its own tamper tests (`tests/test_verify_ledger.py`): the checker is
proven to FAIL on a doctored P&L, altered hours, a leaked receipt, an orphan
settlement, and a rewritten manifest — a re-derivation that cannot fail proves
nothing.

## 3. The no-cherry-picking claim

Both audit files are append-only: every receipt ever committed has a
settlement or a documented missed day. MISSED days stay in the record
(2026-08-05 and 2026-08-07 are real examples). LOSING days will stay
identically when they occur — as of 2026-08-21 the primary has none in 21
settled days, a fact we flag rather than celebrate: the sample is one
solar-heavy August, and our own 11.5-year backtest says winter brings
losing days. Strategy changes require a new
pre-registered strategy id; old receipts stand. Stop conditions and the
comparison bar are pre-registered in GOVERNANCE.md (the public extract of
the operating constitution); strategy-vs-strategy claims must clear that
bar (`scripts/compare_strategies.py` in this repository).

## 4. What this ledger does NOT claim

Paper trading — no market participation, execution assumed at the clearing
price (realistic for 1 MW, a price-taker). The 0.5 EUR/MWh fee covers
exchange fees only: no grid charges, taxes, or aggregator margin, so absolute
EUR is an upper bound on what a real asset would net. Relative metrics
(capture, tau, strategy deltas) are robust to the FEE assumption — but not to
dispatch order, which is the larger caveat (next paragraph). Hourly frame; the
market itself clears at 15-minute granularity since 2025-10-01.

**The P&L does not enforce dispatch order** (disclosed 2026-11-11; found by the
project's red-team review of 2026-10-01). The settlement formula pairs each
day's two sell hours with the same day's two buy hours and ignores which comes
first. On a day when a strategy's sell hours fall before its buy hours, a
battery that starts the day empty cannot run that schedule; one that starts it
charged is selling energy it bought on another day, at another price. The
oracle is computed the same way, so "perfect hindsight" is sometimes an
unexecutable schedule too.

`scripts/feasibility_report.py` measures the effect from the audit files alone,
under two readings. *Continuous*: one battery follows every receipt in time
order and carries its charge across midnight. *Daily-reset*: the battery is
empty at every midnight — the strict reading. Snapshot over all settled days
through target 2026-11-11 (GB through 2026-11-09; run the script for today's
figures), recomputed independently before publication; the last two columns
are the share of the booked P&L the battery could actually dispatch:

| Market | Strategy | Settled days | Out-of-order days | Booked | Continuous | Daily-reset |
|---|---|---|---|---|---|---|
| ES | persistence (primary) | 102 | 21 | 29,946 EUR | 91% | 86% |
| ES | climatology | 101 | 0 | 30,434 EUR | 100% | 100% |
| ES | rankblend | 92 | 10 | 27,846 EUR | 96% | 94% |
| ES | weekly | 81 | 19 | 23,304 EUR | 88% | 80% |
| DE | persistence (primary) | 76 | 21 | 15,651 EUR | 86% | 70% |
| DE | climatology | 80 | 0 | 18,655 EUR | 100% | 100% |
| DE | rankblend | 76 | 7 | 16,611 EUR | 98% | 95% |
| DE | weekly | 79 | 21 | 16,920 EUR | 89% | 64% |
| GB | climatology | 71 | 0 | 8,700 GBP | 100% | 100% |
| GB | weekly | 71 | 23 | 7,332 GBP | 76% | 20% |
| ERCOT | persistence (primary) | 72 | 7 | 5,238 USD | 99% | 96% |
| ERCOT | climatology | 72 | 0 | 5,474 USD | 100% | 100% |
| ERCOT | rankblend | 72 | 1 | 5,481 USD | 100% | 100% |
| ERCOT | weekly | 72 | 6 | 4,953 USD | 97% | 94% |

What this means:

- **Absolute figures** overstate what the simulated battery could have
  dispatched by the shares above, on top of the fee caveat.
- **Comparisons between strategies are NOT robust to it.** Climatology has no
  out-of-order day in any market; persistence and weekly have many. In Spain the
  booked totals put climatology and persistence within a few hundred EUR of each
  other; on the executable totals above climatology is ahead by about 3,000
  (continuous) to 4,800 EUR (daily-reset).
- **What it does not touch:** the receipts, their timing, and the arithmetic of
  the booked formula. Every ledger row still re-derives exactly (section 2).
- **Decision rules:** the pre-registered comparisons and promotion rules are
  defined on the booked P&L. Whether an executable-dispatch metric joins or
  replaces it is an open decision; it will be pre-registered before it is used
  for any verdict.

Two disclosures the verified-track-record industry has taught us to make
explicitly (see docs/research-track-records-2026-08.md): these are SIGNAL
records, not real-money execution records — the distinction that
QuantConnect's Alpha Streams glossed and later regretted. And "independent"
here means independent of the STRATEGY (nothing can be revised after
commitment); the ledger is operated by the project itself — the
OpenTimestamps anchoring exists precisely so that operator role never has to
be taken on trust. Two structural guarantees follow from the same industry's
failure modes: records never start before their first pre-committed receipt
(no backfill), and a retired strategy's settled history is never removed
(no survivorship laundering).
