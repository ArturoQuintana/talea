# Talea — a committed-before-truth day-ahead battery track record, across markets

[![verify](https://github.com/ArturoQuintana/talea/actions/workflows/verify.yml/badge.svg)](https://github.com/ArturoQuintana/talea/actions/workflows/verify.yml)

**Live dashboard:** see `index.html` (GitHub Pages) — a landing page linking one
page per market · **Audit it yourself:** [VERIFY.md](VERIFY.md)

The badge above is not decorative: GitHub re-runs `scripts/verify_ledger.py --all`
on this repository daily and on every update, independently re-deriving every
settlement in every market from the raw prices and receipts. Green means the
numbers reconcile; you can run the identical command yourself (see [VERIFY.md](VERIFY.md)).

This repository is the public, auditable record of a paper-trading experiment on
several day-ahead electricity markets. Every day, BEFORE each market's auction
publishes tomorrow's prices (in Great Britain, where the feed is not an auction:
before any price for the target day exists — see the GB note in
[VERIFY.md](VERIFY.md)), a set of pre-registered strategies commits receipts
simulating a 1 MW / 2 MWh battery (buy the 2 cheapest hours, sell the 2 dearest;
85% round-trip efficiency; explicit fees). Settlements against the published
prices are appended to a ledger that is never edited.

## Markets

Each market lives under `Data/<slug>/` and has its own page (`<slug>.html`):

| Market | Zone | Currency | Source (licence) |
|--------|------|----------|------------------|
| Spain (ES) | Spain | EUR | apidatos.ree.es — public |
| Germany (DE) | DE-LU | EUR | SMARD.de — CC BY 4.0 |
| Great Britain (GB) | GB | GBP | Elexon BMRS Insights — open data |
| ERCOT | HB_NORTH (Texas) | USD | ERCOT MIS NP4-190 — public/redistributable |

Spain is the primary, longest-running market; the others run the identical loop.
Newly launched markets show as *awaiting first settled day* until their first
target day publishes — the committed receipts are already on the page, which is
the point: you see the decision before you see the outcome.

## What makes this record different from a backtest or a vendor claim

- **Committed before truth**: a receipt for day T exists only if it was recorded
  before T's prices were published — enforced in code (the leak guard) and
  provable from the outside.
- **Tamper-evident**: daily OpenTimestamps proofs anchor the audit files' hashes
  in Bitcoin (`Data/<slug>/ots/`, where a market has them; see VERIFY.md for the
  per-market attestation tier).
- **Append-only, losses included**: missed days and losing days stay in the
  record forever. Strategy changes require a new pre-registered strategy id.
- **Pre-registered stop conditions and comparison tests**: the conditions under
  which this experiment is WRONG were published before the data that could
  trigger them.

Metrics reported: money net of costs; **capture** (realized P&L ÷
perfect-hindsight P&L, same battery, same costs); **Kendall tau** (rank quality
of each committed forecast); regime telemetry (negative-price hours,
top-bottom-2 spread).

Honesty note: absolute currency figures are an upper bound — fees cover the
exchange only, not grid charges, taxes, or aggregator margin. Relative metrics
(capture, tau) are robust to the fee assumption. A second caveat is larger: the
P&L does not enforce dispatch order. On some days a strategy's sell hours come
before its buy hours, which a battery that starts the day empty cannot run, and
comparisons between strategies are NOT robust to that — see VERIFY.md section 4
for the measured effect. Prices come from each market's public source
above; Spain is cross-checked weekly against the independent token-based ESIOS
route.

Run the tests: `uv sync && uv run pytest`. Tests that need files kept in the
private repository skip and name the file. The ledger itself re-derives with
`python scripts/verify_ledger.py --all`. Then follow [VERIFY.md](VERIFY.md).
