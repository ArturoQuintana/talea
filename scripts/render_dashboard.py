"""Render the public ledger dashboard (static index.html) from Data/.

Everything is computed from the audit files — no hand-maintained numbers.
Run after each tick (server) or manually:
    uv run python scripts/render_dashboard.py [out_path]
Default out: site/index.html (the mirror publishes it via GitHub Pages).
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "Data"
PRIMARY = "battery-2h2h-persistence"
NAMES = {"battery-2h2h-persistence": "Persistence v1",
         "battery-2h2h-climatology": "Climatology v1",
         "battery-2h2h-rankblend": "Rank-blend v1",
         "battery-2h2h-weekly": "Weekly v1"}
GATE_DAYS = 21
CUR_SYMBOL = {"EUR": "€", "GBP": "£", "USD": "$", "JPY": "¥"}

# Per-market presentation is DERIVED from the market registry (the single source
# of truth) — not a hardcoded dict (Phase 0, 2026-08-27). Every renderable market
# appears automatically; ES presentation strings live on the ES market and are
# byte-for-byte what this dict used to hold, so the public page is unchanged.
sys.path.insert(0, str(ROOT / "src"))
from talea.markets import (   # noqa: E402
    MARKETS as _REGISTRY, public_markets as _public_markets)


def _presentation_config() -> dict[str, dict]:
    out = {}
    for slug, m in _REGISTRY.items():
        p = m.presentation
        if not p.title:
            continue
        out[slug] = {"data": m.prices_path.parent, "title": p.title,
                     "tzlabel": p.tz_label, "gate": p.show_gate,
                     "source": p.source, "tab": p.tab_name,
                     "currency": m.currency,
                     "publication": p.publication, "note": p.note}
    return out


MARKETS = _presentation_config()

# Generic fallback for a market whose registry entry carries no publication
# label — deliberately vague rather than borrowing another market's clock
# (the footer used to hardcode Spain's "~13:15 CET" for every market; audit
# finding 2026-09-07).
GENERIC_PUBLICATION = "the target day's prices are published"


def _publication(cfg: dict) -> str:
    return cfg.get("publication") or GENERIC_PUBLICATION


def _disclosure(cfg: dict) -> str:
    """A market-specific disclosure banner (registry `presentation.note`),
    rendered verbatim on every template of that market's page; empty for
    markets with nothing to disclose."""
    note = cfg.get("note")
    if not note:
        return ""
    return (f'<div class="banner disclosure"><b>Disclosure.</b> {note}</div>')


def jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def day_curves(data: Path = DATA) -> dict[str, list[float | None]]:
    by: dict[str, dict[int, float]] = {}
    for r in json.loads((data / "prices.json").read_text()):
        d, h = r["ts"].split("T")
        by.setdefault(d, {})[int(h)] = r["price"]
    return {d: [v.get(h) for h in range(24)] for d, v in by.items()}


def fmt(x: float) -> str:
    return f"{x:,.2f}"


# Shared design tokens for the landing index and the awaiting-market pages (the
# full per-market TEMPLATE carries its own copy). These f-string-embedded blocks
# are NOT %-formatted, so literal % needs no escaping.
BASE_TOKENS = """<style>
  :root { color-scheme: light;
    --bg:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
    --muted:#898781; --grid:#e1e0d9; --border:rgba(11,11,11,.10);
    --good:#006300; --accent:#2a78d6;
    --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace; }
  @media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) {
    color-scheme:dark; --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --grid:#2c2c2a; --border:rgba(255,255,255,.10); --good:#0ca30c; --accent:#3987e5; } }
  :root[data-theme="dark"] {
    color-scheme:dark; --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --grid:#2c2c2a; --border:rgba(255,255,255,.10); --good:#0ca30c; --accent:#3987e5; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
    line-height:1.5; -webkit-font-smoothing:antialiased; }
  a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
  .wrap { max-width:920px; margin:0 auto; padding:40px 22px 64px; }
  header { margin-bottom:26px; }
  .eyebrow { font:600 11px/1 var(--mono); letter-spacing:.14em; text-transform:uppercase;
    color:var(--muted); margin:0 0 12px; }
  h1 { font-size:30px; line-height:1.15; margin:0 0 12px; letter-spacing:-.02em;
    text-wrap:balance; }
  h2 { font-size:15px; margin:34px 0 12px; letter-spacing:-.01em; }
  .asof { color:var(--ink2); font-size:13.5px; margin:0; max-width:60ch; }
  code { font-family:var(--mono); font-size:.92em; }
  .banner { background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:14px 16px; font-size:13.5px; color:var(--ink2); margin:22px 0 6px; }
  .banner b { color:var(--ink); }
  .banner.disclosure { border-color:var(--accent); }
  .tbl-wrap { overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--card); }
  table { border-collapse:collapse; width:100%; min-width:480px; font-size:13px; }
  th, td { text-align:right; padding:9px 14px; border-top:1px solid var(--grid); white-space:nowrap; }
  th { border-top:0; font:600 10.5px/1 var(--mono); letter-spacing:.1em; text-transform:uppercase; color:var(--muted); }
  th:first-child, td:first-child { text-align:left; }
  td { font-family:var(--mono); font-size:12.5px; font-variant-numeric:tabular-nums; color:var(--ink2); }
  td.k { color:var(--ink); }
  .note { color:var(--ink2); font-size:13px; margin:10px 2px 0; }
  .grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }
  .mkt { display:block; background:var(--card); border:1px solid var(--border);
    border-radius:12px; padding:18px 18px 16px; color:inherit; transition:border-color .12s; }
  .mkt:hover { border-color:var(--accent); text-decoration:none; }
  .mkt .name { font-size:16px; font-weight:600; color:var(--ink); margin:0 0 2px; }
  .mkt .zone { font:11px/1 var(--mono); letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); }
  .mkt .big { font-size:26px; font-weight:600; margin:14px 0 2px; font-variant-numeric:tabular-nums; }
  .mkt .big.pos { color:var(--good); }
  .mkt .sub { font-size:12.5px; color:var(--ink2); }
  .mkt .status { display:inline-block; margin-top:12px; font:11px/1 var(--mono);
    letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }
  .open-card { background:var(--card); border:1px solid var(--border); border-radius:10px;
    padding:12px 14px; display:flex; flex-wrap:wrap; gap:6px 14px; align-items:baseline; }
  .open-card .pending { font:600 10.5px/1 var(--mono); letter-spacing:.1em; text-transform:uppercase;
    color:var(--accent); }
  .open-card .oc { font-size:12.5px; color:var(--ink2); }
  .open-card .m, .open-card b.m { font-family:var(--mono); font-size:12px; color:var(--ink); }
  footer { margin-top:44px; color:var(--muted); font-size:12.5px; line-height:1.7; }
</style>"""


def _open_cards(receipts: list[dict], settled_keys: set) -> list[str]:
    """The 'committed before truth' cards — receipts with no settlement yet."""
    open_receipts = [r for r in receipts
                     if (r["target"], r["strategy"]) not in settled_keys]
    open_html = []
    for r in sorted(open_receipts, key=lambda r: (r["target"], r["strategy"])):
        open_html.append(
            f'<div class="open-card" style="margin-bottom:10px">'
            f'<span class="pending">Pending</span>'
            f'<span class="oc"><b>{r["target"]}</b> · {NAMES.get(r["strategy"], r["strategy"])}'
            f'{" · primary" if r["strategy"] == PRIMARY else " · shadow"}</span>'
            f'<span class="oc">buy <b class="m">{"·".join(f"{h:02d}" for h in r["buy_hours"])}h</b>'
            f' · sell <b class="m">{"·".join(f"{h:02d}" for h in r["sell_hours"])}h</b></span>'
            f'<span class="oc">committed <span class="m">{r["committed_at"][:16]}Z</span>,'
            f' before publication</span></div>')
    return open_html


def _page_head(slug: str, cfg: dict, now: datetime) -> str:
    asof = now.strftime(f"%Y-%m-%d %H:%M {cfg['tzlabel']}")
    return f"""{BASE_TOKENS}<title>{cfg['tab']} day-ahead ledger</title>
<div class="wrap">
  <header>
    <p class="eyebrow"><a href="index.html">Talea</a> · {slug.upper()}</p>
    <h1>{cfg['title']}</h1>
    <p class="asof">1&nbsp;MW / 2&nbsp;MWh virtual battery · decisions committed
      before price publication (leak-guarded, OpenTimestamps-stamped —
      Bitcoin&#8209;confirmed within days) · as of <code>{asof}</code></p>
  </header>
  {_disclosure(cfg)}"""


def _light_footer(cfg: dict) -> str:
    src = cfg.get("source", "")
    return f"""  <footer>
    <p>Paper money — no capital at stake. Prices: {src}. Every receipt is
      committed and pushed before {_publication(cfg)}. The ledger is
      append-only; each day's hash is OpenTimestamps-stamped at commit and
      Bitcoin-confirmed within days (a just-committed proof is <i>pending</i>
      until a block lands — see VERIFY.md for per-date status). Verify
      everything yourself — see VERIFY.md in this repository.</p>
  </footer>
</div>"""


def _awaiting(slug: str, cfg: dict, receipts: list[dict], now: datetime,
              cur: str) -> str:
    """Public page for a live market with NO settled day yet (for ANY strategy):
    the receipts are already committed-before-truth; only the settlement/P&L is
    pending. This is the honest live state of a just-launched market."""
    open_html = _open_cards(receipts, set())
    if not open_html:
        open_html = ['<div class="open-card"><span class="pending">None yet'
                     '</span><span class="oc">First receipt commits at the next '
                     'pre-auction tick; watch this page.</span></div>']
    return f"""{_page_head(slug, cfg, now)}
  <div class="banner"><b>Live · awaiting first settled day.</b> The receipts
    below are already committed and pushed <i>before</i> {_publication(cfg)} —
    that is the whole point. Settlement (P&amp;L vs the realised prices) appears
    here the day after the first target day publishes; nothing is backfilled.</div>
  <h2>Committed receipts · awaiting settlement</h2>
  {chr(10).join(open_html)}
{_light_footer(cfg)}"""


def _missed_days(receipts: list[dict], strategy: str) -> list[str]:
    """Dates in [first receipt target, last receipt target] with no receipt for
    `strategy`. Bounded by the last RECEIPT (not the last SETTLED day) so a
    commit gap surfaces immediately instead of waiting a full settlement cycle
    (independent-audit finding, 2026-08-31). Empty if the strategy has never
    committed at all (that case is disclosed separately, not counted as gaps)."""
    targets = {r["target"] for r in receipts if r["strategy"] == strategy}
    if not targets:
        return []
    d0, d1 = date.fromisoformat(min(targets)), date.fromisoformat(max(targets))
    missed, d = [], d0
    while d <= d1:
        if d.isoformat() not in targets:
            missed.append(d.isoformat())
        d += timedelta(days=1)
    return missed


def _h2h(ledger: list[dict], missed: list[str], cur: str):
    """The strategy-panel table: one row per settled day, one cell per strategy
    that has ever settled; plus the pairwise-vs-primary totals on shared days.
    Shared by the full page and the shadow-only page so shadow settlements are
    rendered identically whether or not the primary has settled."""
    strategies = [s for s in NAMES if any(e["strategy"] == s for e in ledger)]
    by_day: dict[str, dict[str, dict]] = {}
    for e in ledger:
        by_day.setdefault(e["target"], {})[e["strategy"]] = e
    rows = []
    for t in sorted(set(list(by_day) + missed)):
        cells = []
        for s in strategies:
            e = by_day.get(t, {}).get(s)
            if e is None:
                cells.append("<td>missed</td>" if s == PRIMARY and t in missed
                             else "<td>—</td>")
            else:
                cap = (f"{e['capture'] * 100:.1f}%"
                       if e.get("capture") is not None else "n/a")
                tau = (f" · tau {e['tau']:.3f}"
                       if e.get("tau") is not None else "")
                cells.append(f"<td>+{fmt(e['pnl_eur'])}&thinsp;{cur} ({cap}{tau})"
                             "</td>")
        rows.append(f'<tr><td class="k">{t}</td>{"".join(cells)}</tr>')
    pair_notes = []
    for s in strategies:
        if s == PRIMARY:
            continue
        shared = [(by_day[t][s]["pnl_eur"], by_day[t][PRIMARY]["pnl_eur"])
                  for t in by_day if s in by_day[t] and PRIMARY in by_day[t]]
        if shared:
            delta = sum(a - b for a, b in shared)
            pair_notes.append(f"{NAMES[s]} vs {NAMES[PRIMARY]}: "
                              f"{delta:+.2f}&thinsp;{cur} over {len(shared)} "
                              "shared days")
    head = "".join(f"<th>{NAMES[s]}{' · primary' if s == PRIMARY else ' · shadow'}</th>"
                   for s in strategies)
    return head, rows, pair_notes, strategies


def _shadow_only(slug: str, cfg: dict, receipts: list[dict], ledger: list[dict],
                 now: datetime, cur: str) -> str:
    """Public page for a market where SHADOW strategies have settled days but the
    PRIMARY has none. Renders the settled shadow days (the strategy panel table)
    and marks only genuinely-unsettled receipts as pending — never the
    'awaiting first settled day' banner, which would contradict the ledger
    (independent-audit finding F1, 2026-09-07: GB showed 'awaiting' + 16
    'Pending' receipts while 12 shadow settlements sat in ledger.jsonl)."""
    settled_keys = {(e["target"], e["strategy"]) for e in ledger}
    n_days = len({e["target"] for e in ledger})
    prim_receipts = [r for r in receipts if r["strategy"] == PRIMARY]
    n_targets = len({r["target"] for r in receipts})
    if prim_receipts:
        prim_line = (f"The primary ({NAMES[PRIMARY]}) has committed receipts "
                     "but no settled day yet.")
    else:
        prim_line = (f"The primary ({NAMES[PRIMARY]}) has committed <b>no "
                     f"receipt</b> in this market so far (0 of {n_targets} "
                     "target days) — only shadow strategies have; see the "
                     "disclosure above and VERIFY.md.")
    head, rows, pair_notes, strategies = _h2h(ledger, [], cur)
    open_html = _open_cards(receipts, settled_keys)
    if not open_html:
        open_html = ['<div class="open-card"><span class="pending">None open'
                     '</span><span class="oc">Next commit at the next '
                     'pre-deadline tick.</span></div>']
    return f"""{_page_head(slug, cfg, now)}
  <div class="banner"><b>Live · {n_days} settled day{'s' if n_days != 1 else ''} on shadow strategies
    · no primary settlement yet.</b> {prim_line}
    Every receipt below was committed and pushed <i>before</i>
    {_publication(cfg)}; settlements are appended the day after each target
    day completes; nothing is backfilled.</div>
  <h2>Strategy panel · settled days</h2>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Target day</th>{head}</tr></thead>
    <tbody>{chr(10).join(rows)}</tbody>
  </table></div>
  <p class="note">{" · ".join(pair_notes) or "no shared primary/shadow days yet"}.
    Claims of superiority require the pre-registered bar (&ge;30 non-tied shared
    days, sign-test p&lt;0.05) — see VERIFY.md. All {len(strategies)} settled
    strategies use identical costs; none can be revised after the fact.</p>
  <h2>Open receipts · awaiting settlement</h2>
  {chr(10).join(open_html)}
{_light_footer(cfg)}"""


def build(slug: str = "es") -> str:
    cfg = MARKETS[slug]
    DATA = cfg["data"]
    cur = CUR_SYMBOL.get(cfg["currency"], cfg["currency"])
    curcode = cfg["currency"]
    ledger = jsonl(DATA / "ledger.jsonl")
    receipts = jsonl(DATA / "receipts.jsonl")
    now = datetime.now(ZoneInfo("Europe/Madrid"))

    prim = [e for e in ledger if e["strategy"] == PRIMARY]
    if not ledger:
        return _awaiting(slug, cfg, receipts, now, cur)
    if not prim:
        # Shadows have settled, the primary hasn't: the ledger is NOT empty, so
        # the awaiting page would contradict it (audit finding F1, 2026-09-07).
        return _shadow_only(slug, cfg, receipts, ledger, now, cur)
    curves = day_curves(DATA)
    prim_by_day = {e["target"]: e for e in prim}
    total = sum(e["pnl_eur"] for e in prim)
    oracle_total = sum(e["oracle_pnl_eur"] for e in prim)
    caps = [e["capture"] for e in prim if e.get("capture") is not None]
    cap_mean = statistics.fmean(caps) * 100 if caps else 0
    wins = sum(1 for e in prim if e["pnl_eur"] > 0)

    missed = _missed_days(receipts, PRIMARY)

    settled_keys = {(e["target"], e["strategy"]) for e in ledger}

    # day cards for the primary (curve + basis curve joined from receipts)
    prim_receipt_by_target = {r["target"]: r for r in receipts
                              if r["strategy"] == PRIMARY}
    days_js = []
    for e in prim:
        t = e["target"]
        rec = prim_receipt_by_target.get(t, {})
        basis_day = rec.get("basis_day")
        if t not in curves or basis_day not in curves:
            continue
        if any(v is None for v in curves[t] + curves[basis_day]):
            continue
        days_js.append({
            "target": t,
            "weekday": date.fromisoformat(t).strftime("%A"),
            "buy": e["buy_hours"], "sell": e["sell_hours"],
            "pnl": e["pnl_eur"], "oracle": e["oracle_pnl_eur"],
            "capture": e["capture"], "tau": e.get("tau"),
            "prices": curves[t], "basis": curves[basis_day],
            "basisDate": basis_day,
        })

    # head-to-head: one row per settled day, one cell per strategy
    h2h_head, h2h_rows, pair_notes, strategies = _h2h(ledger, missed, cur)

    ledger_rows = []
    for e in prim:
        buy_avg = statistics.fmean(e["buy_prices"])
        sell_avg = statistics.fmean(e["sell_prices"])
        cap = (f"{e['capture'] * 100:.1f}%"
               if e.get("capture") is not None else "n/a")
        ledger_rows.append(
            f'<tr><td class="k">{e["target"]}</td>'
            f'<td>{", ".join(f"{h:02d}" for h in e["buy_hours"])}</td>'
            f"<td>{fmt(buy_avg)}</td>"
            f'<td>{", ".join(f"{h:02d}" for h in e["sell_hours"])}</td>'
            f"<td>{fmt(sell_avg)}</td>"
            f'<td class="pos">+{fmt(e["pnl_eur"])}</td>'
            f"<td>{fmt(e['oracle_pnl_eur'])}</td><td>{cap}</td></tr>")
    for m in missed:
        ledger_rows.append(
            f'<tr><td class="k">{m}</td><td colspan="6" style="text-align:'
            'left">missed — no receipt committed before the window closed; '
            "never backfilled (leak guard)</td><td>—</td></tr>")

    open_html = _open_cards(receipts, settled_keys)
    if not open_html:
        open_html = ['<div class="open-card"><span class="pending">None open'
                     '</span><span class="oc">Next commit at the next 11:00 '
                     'Europe/Madrid tick.</span></div>']

    if cfg["gate"]:
        gate_cells = "".join(f'<i class="{"done" if i < len(prim) else ""}"></i>'
                             for i in range(GATE_DAYS))
        gate_tile = (
            '<div class="tile"><div class="k">GBM v2 gate</div>'
            f'<div class="v">{min(len(prim), GATE_DAYS)} / {GATE_DAYS}</div>'
            '<div class="s">settled days · evaluate ~21 Aug</div></div>')
        gate_section = (
            '<h2>Escalation gate</h2><div class="gate">'
            '<div class="gl"><span>Progress to the GBM&nbsp;v2 evaluation</span>'
            f'<span class="m">{min(len(prim), GATE_DAYS)} of {GATE_DAYS} settled days</span></div>'
            f'<div class="gate-cells">{gate_cells}</div>'
            '<div class="gl"><span>Evaluation criteria frozen in advance; stop '
            'conditions pre-registered</span><span class="m">~21 Aug 2026</span></div></div>')
    else:
        gate_tile = (
            '<div class="tile"><div class="k">Market</div>'
            f'<div class="v">{slug.upper()}</div>'
            '<div class="s">public ledger</div></div>')
        gate_section = ""

    return TEMPLATE % {
        "tabtitle": cfg["tab"] + " day-ahead ledger",
        "title": cfg["title"],
        "cur": cur, "curcode": curcode,
        "source": cfg.get("source", "apidatos.ree.es"),
        "publication": _publication(cfg),
        "disclosure": _disclosure(cfg),
        "asof": now.strftime(f"%Y-%m-%d %H:%M {cfg['tzlabel']}"),
        "total": fmt(total), "oracle_total": fmt(oracle_total),
        "cap_mean": f"{cap_mean:.1f}", "wins": wins, "n": len(prim),
        "missed_n": len(missed),
        "gate_tile": gate_tile, "gate_section": gate_section,
        "days_json": json.dumps(days_js),
        "open_cards": "\n".join(open_html),
        "h2h_head": h2h_head, "h2h_rows": "\n".join(h2h_rows),
        "pair_notes": " · ".join(pair_notes) or "no shared shadow days yet",
        "n_strategies": len(strategies),
        "ledger_rows": "\n".join(ledger_rows),
    }


TEMPLATE = """<title>%(tabtitle)s</title>
<style>
  :root { color-scheme: light;
    --bg:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
    --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
    --border:rgba(11,11,11,.10); --buy:#2a78d6; --sell:#eb6834;
    --good:#006300; --bad:#d03b3b;
    --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
    --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace; }
  @media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) {
    color-scheme:dark; --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --buy:#3987e5; --sell:#d95926; --good:#0ca30c; } }
  :root[data-theme="dark"] {
    color-scheme:dark; --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --buy:#3987e5; --sell:#d95926; --good:#0ca30c; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 var(--sans); }
  .wrap { max-width:940px; margin:0 auto; padding:40px 20px 56px; }
  .eyebrow { font:600 11px/1 var(--mono); letter-spacing:.14em; text-transform:uppercase; color:var(--muted); }
  h1 { font:600 26px/1.25 var(--sans); margin:8px 0 4px; text-wrap:balance; }
  .asof { color:var(--ink2); font-size:13.5px; margin:0; }
  .asof code { font-family:var(--mono); font-size:12.5px; }
  h2 { font:600 12px/1 var(--mono); letter-spacing:.12em; text-transform:uppercase;
    color:var(--ink2); margin:40px 0 14px; display:flex; align-items:center; gap:12px; }
  h2::after { content:""; flex:1; height:1px; background:var(--grid); }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; margin-top:26px; }
  .tile { background:var(--card); border:1px solid var(--border); border-radius:6px; padding:14px 16px 12px; }
  .tile .k { font:600 10.5px/1 var(--mono); letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
  .tile .v { font:600 27px/1.15 var(--sans); margin-top:7px; }
  .tile .v.pos { color:var(--good); }
  .tile .s { color:var(--ink2); font-size:12.5px; margin-top:3px; }
  .day { background:var(--card); border:1px solid var(--border); border-radius:6px;
    padding:18px 18px 12px; margin-bottom:16px; }
  .day-head { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px; margin-bottom:4px; }
  .day-head .date { font-weight:600; font-size:16px; }
  .day-head .wd { color:var(--muted); font-size:13px; }
  .chips { margin-left:auto; display:flex; gap:8px; }
  .chip { font:500 12.5px/1 var(--mono); padding:5px 9px; border-radius:99px;
    border:1px solid var(--border); color:var(--ink2); white-space:nowrap; }
  .chip.pnl-pos { color:var(--good); border-color:color-mix(in srgb,var(--good) 35%%,transparent); }
  .chip.pnl-neg { color:var(--bad); border-color:color-mix(in srgb,var(--bad) 35%%,transparent); }
  .day-sub { color:var(--ink2); font-size:13px; margin:0 0 10px; }
  .day-sub .m { font-family:var(--mono); font-size:12px; }
  .legend { display:flex; flex-wrap:wrap; gap:6px 18px; margin:2px 0 24px; align-items:center; }
  .legend .li { display:inline-flex; align-items:center; gap:7px; font-size:12.5px; color:var(--ink2); }
  .chart-box { position:relative; }
  .chart-box svg { display:block; width:100%%; height:auto; }
  .tip { position:absolute; pointer-events:none; display:none; background:var(--card);
    border:1px solid var(--border); border-radius:5px; box-shadow:0 2px 10px rgba(0,0,0,.14);
    font:500 12px/1.5 var(--mono); color:var(--ink); padding:7px 10px; white-space:nowrap; z-index:3; }
  .tip .t2 { color:var(--ink2); }
  .open-card { background:var(--card); border:1px solid var(--border); border-radius:6px;
    padding:16px 18px; display:flex; flex-wrap:wrap; gap:10px 26px; align-items:baseline; }
  .open-card .oc { font-size:13.5px; color:var(--ink2); }
  .open-card .oc b { color:var(--ink); font-weight:600; }
  .open-card .oc .m { font-family:var(--mono); font-size:12.5px; }
  .pending { font:600 10.5px/1 var(--mono); letter-spacing:.1em; text-transform:uppercase;
    color:var(--ink2); border:1px solid var(--axis); border-radius:99px; padding:5px 9px; }
  .note { color:var(--ink2); font-size:13px; margin:10px 2px 0; }
  .banner { background:var(--card); border:1px solid var(--border); border-radius:6px;
    padding:14px 16px; font-size:13.5px; color:var(--ink2); margin:22px 0 6px; }
  .banner b { color:var(--ink); }
  .gate { background:var(--card); border:1px solid var(--border); border-radius:6px; padding:16px 18px; }
  .gate-cells { display:grid; grid-template-columns:repeat(21,1fr); gap:4px; margin:10px 0 8px; }
  .gate-cells i { display:block; height:14px; border-radius:3px; background:var(--bg); border:1px solid var(--grid); }
  .gate-cells i.done { background:var(--buy); border-color:var(--buy); }
  .gate .gl { display:flex; justify-content:space-between; color:var(--ink2); font-size:12.5px; }
  .gate .gl .m { font-family:var(--mono); font-size:12px; }
  .tbl-wrap { overflow-x:auto; border:1px solid var(--border); border-radius:6px; background:var(--card); }
  table { border-collapse:collapse; width:100%%; min-width:640px; font-size:13px; }
  th, td { text-align:right; padding:9px 14px; border-top:1px solid var(--grid); white-space:nowrap; }
  th { border-top:0; font:600 10.5px/1 var(--mono); letter-spacing:.1em; text-transform:uppercase; color:var(--muted); }
  th:first-child, td:first-child { text-align:left; }
  td { font-family:var(--mono); font-size:12.5px; font-variant-numeric:tabular-nums; color:var(--ink2); }
  td.k { color:var(--ink); }
  td.pos { color:var(--good); }
  tfoot td { border-top:2px solid var(--axis); font-weight:600; color:var(--ink); }
  footer { margin-top:44px; color:var(--muted); font-size:12.5px; line-height:1.7; }
  footer .m { font-family:var(--mono); font-size:11.5px; }
</style>

<div class="wrap">
  <header>
    <p class="eyebrow">Talea · paper-trading ledger</p>
    <h1>%(title)s</h1>
    <p class="asof">1 MW / 2 MWh virtual battery · decisions committed before price publication
      (leak-guarded, OpenTimestamps-stamped &mdash; Bitcoin&#8209;confirmed within days) ·
      data as of <code>%(asof)s</code> · generated from the audit files, no hand-edited numbers</p>
  </header>
  %(disclosure)s

  <div class="tiles">
    <div class="tile"><div class="k">Net P&amp;L · paper</div>
      <div class="v pos">+%(total)s&thinsp;%(cur)s</div>
      <div class="s">of %(oracle_total)s&thinsp;%(cur)s oracle ceiling</div></div>
    <div class="tile"><div class="k">Mean capture</div>
      <div class="v">%(cap_mean)s%%</div>
      <div class="s">of perfect-hindsight P&amp;L</div></div>
    <div class="tile"><div class="k">Record</div>
      <div class="v">%(wins)s / %(n)s</div>
      <div class="s">winning settled days · %(missed_n)s missed</div></div>
    %(gate_tile)s
  </div>

  <h2>Settled days · primary strategy</h2>
  <div class="legend" id="legend"></div>
  <div id="days"></div>

  <h2>Open receipts</h2>
  %(open_cards)s

  <h2>Strategy panel · identical settled days</h2>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Target day</th>%(h2h_head)s</tr></thead>
    <tbody>%(h2h_rows)s</tbody>
  </table></div>
  <p class="note">%(pair_notes)s. Claims of superiority require the
    pre-registered bar (&ge;30 non-tied shared days, sign-test p&lt;0.05) —
    see VERIFY.md. All %(n_strategies)s strategies settle on identical days
    with identical costs; none can be revised after the fact.</p>

  %(gate_section)s

  <h2>Ledger · append-only · primary</h2>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Target day</th><th>Buy hours</th><th>Buy avg</th>
      <th>Sell hours</th><th>Sell avg</th><th>P&amp;L</th><th>Oracle</th>
      <th>Capture</th></tr></thead>
    <tbody>%(ledger_rows)s</tbody>
  </table></div>

  <footer>
    <p>Costs are explicit in every figure: 85%% round-trip efficiency and
      0.50&thinsp;%(cur)s/MWh fees on every MWh moved — net is never reported as
      gross. The oracle is the best possible 2×2 hour choice with hindsight,
      same battery, same costs. Capture = P&amp;L ÷ oracle P&amp;L; tau =
      Kendall tau-b of the committed forecast vs the actual day.</p>
    <p>Paper money — no capital at stake. Every receipt is committed and
      pushed before %(publication)s; the ledger is
      append-only and OpenTimestamps-stamped — each day's proof is submitted at
      commit and Bitcoin-confirmed within days (a fresh proof is <i>pending</i>
      until a block lands; see VERIFY.md for per-date confirmed/pending status);
      prices: %(source)s.
      Absolute %(curcode)s is an <b>upper bound</b> (exchange fees only — no grid
      charges, taxes, or aggregator margin); relative metrics are robust.
      Verify everything yourself: see VERIFY.md in this repository.</p>
  </footer>
</div>

<script>
  const DAYS = %(days_json)s;
  const fmt = (x, d = 2) => x.toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const avg = a => a.reduce((s, x) => s + x, 0) / a.length;
  const hh = h => String(h).padStart(2, "0");
  function oraclePicks(p) {
    const idx = p.map((v, i) => i);
    return { cheap: [...idx].sort((a, b) => p[a] - p[b]).slice(0, 2),
             dear:  [...idx].sort((a, b) => p[b] - p[a]).slice(0, 2) };
  }
  const YMAXBASE = 210;
  const YMAX = Math.max(YMAXBASE, ...DAYS.flatMap(d => d.prices)) * 1.02;
  const W = 760, H = 208, padL = 44, padR = 14, padT = 12, padB = 24;
  const xw = W - padL - padR, yh = H - padT - padB;
  const X = h => padL + (h / 23) * xw;
  const Y = v => padT + (1 - v / YMAX) * yh;
  const path = p => p.map((v, h) => (h ? "L" : "M") + X(h).toFixed(1) + " " + Y(v).toFixed(1)).join(" ");
  function dayCard(d) {
    const o = oraclePicks(d.prices);
    let grid = "", xlab = "";
    for (const g of [0, 50, 100, 150, 200]) {
      grid += `<line x1="${padL}" x2="${W - padR}" y1="${Y(g)}" y2="${Y(g)}" stroke="var(--grid)" stroke-width="1"/>`
            + `<text x="${padL - 8}" y="${Y(g) + 3.5}" text-anchor="end" font-size="10.5" fill="var(--muted)" font-family="var(--mono)">${g}</text>`;
    }
    for (const h of [0, 6, 12, 18, 23]) {
      xlab += `<text x="${X(h)}" y="${H - 7}" text-anchor="middle" font-size="10.5" fill="var(--muted)" font-family="var(--mono)">${hh(h)}h</text>`;
    }
    const oracleMarks = [...o.cheap, ...o.dear].map(h =>
      `<circle cx="${X(h)}" cy="${Y(d.prices[h])}" r="6.5" fill="none" stroke="var(--muted)" stroke-width="1.5"/>`).join("");
    const mark = (h, c) => `<circle cx="${X(h)}" cy="${Y(d.prices[h])}" r="5" fill="var(${c})" stroke="var(--card)" stroke-width="2"/>`;
    const lbl = (hs, txt) => {
      const h = hs[0], above = d.prices[h] < YMAX * 0.55;
      return `<text x="${X(h)}" y="${Y(d.prices[h]) + (above ? -12 : 18)}" text-anchor="middle" font-size="10.5" font-weight="600" letter-spacing=".08em" fill="var(--ink2)" font-family="var(--mono)">${txt}</text>`;
    };
    const buyAvg = avg(d.buy.map(h => d.prices[h])), sellAvg = avg(d.sell.map(h => d.prices[h]));
    return `
    <div class="day">
      <div class="day-head">
        <span class="date">${d.target}</span><span class="wd">${d.weekday}</span>
        <span class="chips">
          <span class="chip ${d.pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${d.pnl >= 0 ? "+" : "−"}${fmt(Math.abs(d.pnl))} %(cur)s</span>
          <span class="chip">capture ${d.capture != null ? (d.capture * 100).toFixed(1) + "%%" : "n/a"}</span>
          ${d.tau != null ? `<span class="chip">tau ${d.tau.toFixed(3)}</span>` : ""}
        </span>
      </div>
      <p class="day-sub">Bought <span class="m">${d.buy.map(hh).join("·")}h</span> at avg
        <span class="m">${fmt(buyAvg)} %(cur)s/MWh</span>, sold <span class="m">${d.sell.map(hh).join("·")}h</span> at avg
        <span class="m">${fmt(sellAvg)} %(cur)s/MWh</span> — hours picked from ${d.basisDate}'s profile.
        Oracle best: <span class="m">${fmt(d.oracle)} %(cur)s</span>.</p>
      <div class="chart-box" data-day="${d.target}">
        <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Hourly prices for ${d.target}">
          ${grid}${xlab}
          <line x1="${padL}" x2="${W - padR}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--axis)" stroke-width="1"/>
          <path d="${path(d.basis)}" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="3 4" opacity=".65"/>
          <path d="${path(d.prices)}" fill="none" stroke="var(--ink2)" stroke-width="2" stroke-linejoin="round"/>
          <line class="xh" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="var(--axis)" stroke-width="1" style="display:none"/>
          ${oracleMarks}${d.buy.map(h => mark(h, "--buy")).join("")}${d.sell.map(h => mark(h, "--sell")).join("")}
          ${lbl(d.buy, "BUY")}${lbl(d.sell, "SELL")}
          <circle class="hovpt" r="3.5" fill="var(--ink)" style="display:none"/>
          <rect x="${padL}" y="${padT}" width="${xw}" height="${yh}" fill="transparent"/>
        </svg>
        <div class="tip"></div>
      </div>
    </div>`;
  }
  document.getElementById("days").innerHTML = DAYS.map(dayCard).join("");
  document.getElementById("legend").innerHTML = [
    ['<svg width="22" height="10"><line x1="0" x2="22" y1="5" y2="5" stroke="var(--ink2)" stroke-width="2"/></svg>', "target-day price (%(cur)s/MWh)"],
    ['<svg width="22" height="10"><line x1="0" x2="22" y1="5" y2="5" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="3 4"/></svg>', "basis day — the decision input"],
    ['<svg width="12" height="12"><circle cx="6" cy="6" r="4.5" fill="var(--buy)"/></svg>', "committed buy hours"],
    ['<svg width="12" height="12"><circle cx="6" cy="6" r="4.5" fill="var(--sell)"/></svg>', "committed sell hours"],
    ['<svg width="14" height="14"><circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--muted)" stroke-width="1.5"/></svg>', "oracle's picks (hindsight)"]
  ].map(([sw, t]) => `<span class="li">${sw}${t}</span>`).join("");
  document.querySelectorAll(".chart-box").forEach(box => {
    const d = DAYS.find(x => x.target === box.dataset.day);
    const svg = box.querySelector("svg"), tip = box.querySelector(".tip");
    const xh = svg.querySelector(".xh"), pt = svg.querySelector(".hovpt");
    svg.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect();
      const mx = (e.clientX - r.left) * (W / r.width);
      const h = Math.max(0, Math.min(23, Math.round((mx - padL) / xw * 23)));
      xh.setAttribute("x1", X(h)); xh.setAttribute("x2", X(h)); xh.style.display = "";
      pt.setAttribute("cx", X(h)); pt.setAttribute("cy", Y(d.prices[h])); pt.style.display = "";
      const role = d.buy.includes(h) ? " · BUY" : d.sell.includes(h) ? " · SELL" : "";
      tip.innerHTML = `${hh(h)}:00${role}<br><span class="t2">day&nbsp;</span>${fmt(d.prices[h])} %(cur)s/MWh` +
                      `<br><span class="t2">basis</span> ${fmt(d.basis[h])} %(cur)s/MWh`;
      tip.style.display = "block";
      const bx = (X(h) / W) * r.width;
      tip.style.left = Math.min(r.width - 150, Math.max(4, bx + 12)) + "px";
      tip.style.top = "10px";
    });
    svg.addEventListener("mouseleave", () => {
      tip.style.display = "none"; xh.style.display = "none"; pt.style.display = "none";
    });
  });
</script>
"""


def _primary_slug() -> str:
    """The primary market (its page is index.html; others are <slug>.html)."""
    return next((m.slug for m in _REGISTRY.values() if m.primary), "es")


def _href(slug: str, primary: str) -> str:
    return "index.html" if slug == primary else f"{slug}.html"


NAV_STYLE = """<style>
.talea-nav{display:flex;gap:10px 18px;align-items:baseline;flex-wrap:wrap;
  max-width:920px;margin:0 auto;padding:18px 22px 0;
  font-family:var(--mono,ui-monospace,monospace);}
.talea-nav .brand{font-weight:700;letter-spacing:.04em;font-size:14px;
  color:var(--ink,#111);text-decoration:none;}
.talea-nav .mkts{display:flex;gap:14px;flex-wrap:wrap;}
.talea-nav a{color:var(--ink2,#666);text-decoration:none;font-size:12.5px;
  letter-spacing:.03em;}
.talea-nav a:hover{color:var(--ink,#111);}
.talea-nav a.here{color:var(--ink,#111);font-weight:600;
  border-bottom:2px solid var(--ink,#111);padding-bottom:2px;}
</style>"""


def _nav(slugs: list[str], current: str, primary: str) -> str:
    """A neutral cross-market strip: the Talea wordmark + one link per public
    market (the current one marked). NOT a landing page — each market's own page
    stays first-class; this is only connective tissue so the markets read as one
    project. The primary market keeps the root URL (index.html)."""
    links = []
    for s in slugs:
        here = ' class="here"' if s == current else ''
        links.append(f'<a href="{_href(s, primary)}"{here}>{MARKETS[s]["tab"]}</a>')
    links = "".join(links)
    return (NAV_STYLE
            + f'<nav class="talea-nav"><a class="brand" '
              f'href="{_href(primary, primary)}">Talea</a>'
              f'<span class="mkts">{links}</span></nav>')


def _page(inner: str, nav: str = "") -> str:
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\"></head><body>" + nav + inner + "</body></html>")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    if "--site" in args:
        i = args.index("--site")
        outdir = Path(args[i + 1])
        del args[i:i + 2]
        outdir.mkdir(parents=True, exist_ok=True)
        slugs = [m.slug for m in _public_markets()]
        primary = _primary_slug()
        for slug in slugs:
            page = _page(build(slug), _nav(slugs, slug, primary))
            fname = "index.html" if slug == primary else f"{slug}.html"
            (outdir / fname).write_text(page)
            print(f"rendered {outdir / fname} ({len(page)} bytes)")
        print(f"site: {len(slugs)} markets ({' '.join(slugs)}); "
              f"primary {primary} -> index.html")
        return
    slug = "es"
    if "--market" in args:
        i = args.index("--market")
        slug = args[i + 1]
        del args[i:i + 2]
    out = Path(args[0]) if args else ROOT / ("site/index.html" if slug == "es"
                                             else f"site/{slug}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    html = _page(build(slug))
    out.write_text(html)
    print(f"rendered {out} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
