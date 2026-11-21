# `talea` — package layout

A committed-before-truth paper-trading loop on day-ahead electricity prices, across
several markets. Two patterns organize the code; read them and the tree explains
itself.

```
talea/
  loop.py          THE FUNCTIONAL CORE. Pure domain logic: the tick orchestrator,
                   the leak/clock guards, settlement + P&L math, storage (append-
                   only receipts/ledger, atomic price writes, the writer lock),
                   telemetry. Market-agnostic — it operates on a `Market`, never a
                   specific one. Defines the `Market`/`Presentation` dataclasses.
  __main__.py      THE IMPERATIVE SHELL. Everything effectful the core refuses to
                   touch: the CLI, git backup/push, OpenTimestamps stamping, the
                   email digest, the heartbeat. Injection seams (_smtp, _urlopen,
                   _run) keep it testable.
  markets/         THE MARKET PLUGINS (one subdir per market).
    __init__.py    The registry — the SINGLE SOURCE OF TRUTH. `MARKETS` +
                   the query helpers (`public_markets`, `shadows`, `by_driver`)
                   every operational surface uses instead of hardcoding slugs.
    base.py        The market CONTRACT: the `Fetcher` protocol + the re-exported
                   `Market`/`Presentation` types (a plugin imports its types from
                   here, not by reaching into the core).
    _entsoe.py     A SHARED library (leading underscore = a lib, NOT a market):
                   the ENTSO-E A44 day-ahead client. IT/PT/FR each bind it
                   independently with their own zone + timezone.
    es/  de/  ercot/    DEDICATED-client markets — each owns a `fetch.py` because it
                        has real fetch LOGIC (apidatos, SMARD, the ERCOT MIS
                        pipeline).
    it/  pt/  fr/       SHARED-library markets — no `fetch.py`; their fetch is a
                        one-line EIC/timezone binding of `_entsoe`, inline in
                        `__init__.py` (config, not logic).
```

## The two rules a reader needs

1. **Core vs shell.** `loop.py` decides; `__main__.py` acts. The core never does
   I/O beyond reading/writing its own files; anything that talks to the network,
   git, email, or Bitcoin lives in the shell. (Functional core / imperative shell.)

2. **A market is a plugin.** Everything about a market — its zone EIC, timezone,
   commit deadline, currency, flags (`primary`/`public`/`driver`/`redistributable`),
   presentation, and fetch — lives in `markets/<slug>/`. A market owns a `fetch.py`
   only when it has dedicated fetch *logic*; markets that reuse a shared library
   (`_entsoe`) bind it inline. Adding a market is: drop in `markets/<slug>/`, and
   the registry + every operational surface pick it up. Conformance is enforced by
   `tests/test_market_conformance.py` (every market must pass the same guards).

## Data

Each market's audit trail lives at `Data/<slug>/` (receipts, ledger, prices, OTS
proofs) — ES included, none privileged. `Data/` root holds only shared/project
artifacts (`calibration/`, and the `esios_prices.json` deep history, which the mirror's
current tree does not carry: earlier copies remain in the mirror's git history, see
`DATA-SOURCES.md`). See
`docs/ARCHITECTURE.md` for the full system (writers, mirror, governance) and `CLAUDE.md` for the operating
rules.
