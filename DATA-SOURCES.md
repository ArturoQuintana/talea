# Data sources & licences

Talea publishes price-derived ledgers for several markets. Each market's **price
data** comes from that market's own source, under that source's terms. Each
published market's `Data/<slug>/LICENSE.md` states its source and what is known
of those terms, and this file indexes them. Where no licence is verified, this
file says so: for Spain (ES), see "Spain (ES)" below.

Markets whose price data is known not to be freely redistributable (the
ENTSO-E-sourced IT/PT/FR) are not published here. Two slips are logged in
`docs/incidents.md`: French price files were on this mirror's main for 72
minutes on 2026-08-28, and a Portuguese API response served as a test
fixture until 2026-11-11.

| Market | Directory | Source | Licence / terms | Required attribution |
|--------|-----------|--------|-----------------|----------------------|
| Spain (ES) | `Data/es/` | REE — apidatos.ree.es (REData) | No licence verified: see "Spain (ES)" below | Red Eléctrica de España (REE) |
| Germany (DE) | `Data/de/` | SMARD.de (Bundesnetzagentur) | CC BY 4.0 | Bundesnetzagentur \| SMARD.de |
| Great Britain (GB) | `Data/gb/` | Elexon BMRS Insights | Elexon open data | Elexon / BMRS |
| ERCOT | `Data/ercot/` | ERCOT MIS (NP4-190, DAM SPP) | ERCOT public data | ERCOT |

## Spain (ES): the two Red Eléctrica files

Both files hold prices published by **Red Eléctrica de España (REE)**. No
licence for redistributing either is verified. REData's API page states no
licence; its own words are "use them for your own purposes".
Talea is putting the question of both files to REE in writing.

- `Data/es/prices.json`: hourly Spanish day-ahead prices from REE's REData API
  (apidatos.ree.es), from 2026-01-27, updated by each daily tick (the file's
  latest commit on this mirror gives the date of its last update). Published
  on this mirror.
- `Data/esios_prices.json`: the deep history, daily rows of Spanish day-ahead
  prices from REE's ESIOS API (api.esios.ree.es, token access), from
  2015-01-01, refreshed weekly (last refreshed 2026-11-15). **Not published on
  this mirror**, pending clarification from REE; earlier publishes carried it,
  and it remains in this mirror's git history.

The **authoritative** terms are each source's own — see the per-market
`LICENSE.md` and the linked source for the binding conditions.

Ledger figures are **derived** metrics: the settlement of a simulated
1 MW / 2 MWh battery against these published prices. Talea claims no ownership of
the underlying price data. The Talea **code** (`src/`, `scripts/`, `tests/`) is
licensed separately — see `LICENSE`.
