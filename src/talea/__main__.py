"""CLI: `python -m talea tick` (the daily pass) / `status` (ledger summary)."""
from __future__ import annotations

import hashlib
import json
import os
import smtplib
import statistics
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from math import comb
from pathlib import Path
from types import SimpleNamespace

from .liveness import assess, describe
from .loop import (DATA_DIR, LEDGER, RECEIPTS, STRATEGY, WriterLockError,
                   _load_jsonl, tick, writer_lock)

REPO = Path(__file__).resolve().parents[2]
OTS_DIR = DATA_DIR / "es" / "ots"      # ES now lives under Data/es/ (Stage B)
# Pinned: the attestation path must not silently pull a new OTS client build
# (supply-chain surface on the one thing that makes the ledger tamper-evident).
# Bump deliberately after testing `ots` locally. Mirrored in verify_ledger.py
# and scripts/weekly_maintenance.sh.
OTS_CLIENT = "opentimestamps-client==0.7.2"


def _env(key: str) -> str | None:
    """Environment first, then the repo's .env (KEY=VALUE lines, gitignored)."""
    if key in os.environ:
        return os.environ[key]
    envf = REPO / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


def heartbeat(ok: bool, *, _urlopen=urllib.request.urlopen) -> None:
    """Ping the external healthcheck: success iff TODAY'S RECEIPT EXISTS (the
    one thing the loop must produce daily); /fail when the tick ran but could
    not commit (late wake, incomplete basis) so the alert is immediate rather
    than after grace. No URL configured -> silently off. Never fatal."""
    url = _env("ESIOS_HEARTBEAT_URL")
    if not url:
        return
    try:
        _urlopen(url if ok else url.rstrip("/") + "/fail", timeout=10)
        print(f"[esios-paper] heartbeat pinged{'' if ok else ' (FAIL signal)'}")
    except Exception as exc:
        print(f"[esios-paper] heartbeat ping failed: {exc}")


def _sign_p(wins: int, n: int) -> float:
    """One-sided binomial P(X >= wins | n, 0.5) — the pre-registered test."""
    return sum(comb(n, i) for i in range(wins, n + 1)) / 2 ** n


def _liveness_rows(now: datetime) -> list[dict]:
    """The liveness assessment over every REGISTERED market, read under
    DATA_DIR (so tests can seed a tmp tree) — the registry's own paths are
    absolute and bound to the real Data/ at import time."""
    from .markets import MARKETS
    views = [SimpleNamespace(slug=s, receipts_path=DATA_DIR / s / "receipts.jsonl",
                             prices_path=DATA_DIR / s / "prices.json",
                             ots_dir=DATA_DIR / s / "ots") for s in MARKETS]
    return assess(views, now)


def build_digest(now: datetime | None = None) -> tuple[str, str] | None:
    """(subject, body) for the daily inbox digest — same numbers and house
    rules as the cloud routine: lead with the outcome, never soften the math,
    no superiority claims below the pre-registered bar."""
    ledger = _load_jsonl(LEDGER)
    if not ledger:
        return None
    latest = max(e["target"] for e in ledger)
    today = {e["strategy"]: e for e in ledger if e["target"] == latest}
    prim = [e for e in ledger if e["strategy"] == STRATEGY]
    total = sum(e["pnl_eur"] for e in prim)
    caps = [e["capture"] for e in prim if e.get("capture") is not None]
    wins = sum(1 for e in prim if e["pnl_eur"] > 0)
    losing_today = any(e["pnl_eur"] < 0 for e in today.values())
    crosscheck = (DATA_DIR / "CROSSCHECK-ALERTS.log").exists()

    lines = [f"Settlement {latest}:"]
    for name, e in sorted(today.items()):
        cap = f"{e['capture'] * 100:.1f}%" if e.get("capture") is not None else "n/a"
        tau = f", tau {e['tau']:.3f}" if e.get("tau") is not None else ""
        lines.append(f"  {name}: {e['pnl_eur']:+.2f} EUR ({cap}{tau})")
    lines.append("")
    lines.append(
        f"Primary: {total:+.2f} EUR over {len(prim)} days | "
        f"win rate {wins}/{len(prim)} | mean capture "
        f"{statistics.fmean(caps) * 100:.1f}% | gate {min(len(prim), 21)}/21")

    by_day: dict[str, dict[str, float]] = {}
    for e in ledger:
        by_day.setdefault(e["target"], {})[e["strategy"]] = e["pnl_eur"]
    for shadow in sorted({e["strategy"] for e in ledger} - {STRATEGY}):
        deltas = [v[shadow] - v[STRATEGY] for v in by_day.values()
                  if shadow in v and STRATEGY in v]
        # DESCRIPTIVE ONLY. Ties per rule A (round-to-cents). The p here is the
        # raw iid indicator; it is NOT the operative bar. The bar-met VERDICT is
        # the pre-registered Option C bar (p_eff=max(p_iid,p_boot) + Holm), which
        # is referee-gated and computed by `compare_strategies.py --panel` — the
        # digest never auto-declares it (docs/multiple-comparisons-policy.md).
        w = sum(1 for d in deltas if round(d, 2) >= 0.01)
        l = sum(1 for d in deltas if round(d, 2) <= -0.01)
        n = w + l
        p = _sign_p(w, n) if n else 1.0
        lines.append(
            f"{shadow} vs primary: {sum(deltas):+.2f} EUR over {len(deltas)} "
            f"shared ({w}W-{len(deltas) - w - l}T-{l}L, iid p={p:.3f}) — "
            f"verdict via the Option C bar (compare_strategies --panel)")

    prime = today.get(STRATEGY)
    if prime is not None and prime.get("tb2_spread") is not None:
        week = [e for e in prim if e.get("tb2_spread") is not None][-7:]
        lines.append(
            f"Regime: neg_hours {prime.get('neg_hours')} | tb2 "
            f"{prime['tb2_spread']:.0f} EUR (7d mean "
            f"{statistics.fmean(e['tb2_spread'] for e in week):.0f})")

    # Pillar B in the inbox (2026-10-01): the server tick's liveness alert went
    # only to an ntfy phone push (ephemeral, unaudited), so IT/PT/FR stalled for
    # 33 days while every digest kept printing their last settled line unchanged.
    # The SAME assessment the CLI runs now heads the ALERTS line and flips the
    # subject — durable, read daily, visible to the auditors. Reads DATA_DIR
    # (the test seam) for every REGISTERED market.
    dark = [r for r in _liveness_rows(now or datetime.now(timezone.utc))
            if r["state"] == "STALE"]
    alerts = []
    if dark:
        alerts.append("LIVENESS STALE: " + ", ".join(r["slug"] for r in dark)
                      + " (a registered market has gone dark — see below)")
    if losing_today:
        alerts.append("losing day in latest settlement (data, not a bug)")
    if crosscheck:
        alerts.append("CROSSCHECK-ALERTS.log present — price routes disagreed")
    if dark:
        lines.append("")
        lines.append("Liveness (scripts/check_liveness.py):")
        lines.extend(describe(r) for r in dark)
    if alerts:
        lines.insert(0, "ALERTS: " + "; ".join(alerts))
        lines.insert(1, "")
    subject = (("ALERT — " if alerts else "") +
               f"esios digest {latest} · "
               f"{(prime or {}).get('pnl_eur', 0):+.0f} € · {wins}/{len(prim)}")
    # Shadow markets — this private email only (never published). The set and
    # each market's public/silent label come from the registry, not a hardcoded
    # list (Phase 0). DATA_DIR is kept as the read seam so tests can monkeypatch.
    from .markets import shadows
    for mk in shadows():
        mrows = _load_jsonl(DATA_DIR / mk.slug / "ledger.jsonl")
        if not mrows:
            continue
        mp = [e for e in mrows if e["strategy"] == STRATEGY]
        if not mp:
            continue
        last_t = max(e["target"] for e in mp)
        le = next(e for e in mp if e["target"] == last_t)
        mcaps = [e["capture"] for e in mp if e.get("capture") is not None]
        tag = f"{mk.slug.upper()} {'public' if mk.public else 'silent'}"
        lines.append("")
        lines.append(
            f"[{tag}] {len(mp)} days | total {sum(e['pnl_eur'] for e in mp):+.2f} | "
            f"mean capture {statistics.fmean(mcaps) * 100:.1f}% | "
            f"latest {last_t}: {le['pnl_eur']:+.2f} (cap {le.get('capture')})")
    lines.append("")
    lines.append("Paper money, upper bound (exchange fees only).")
    lines.append("Dashboards: Spain arturoquintana.github.io/spain-dayahead-ledger"
                 " · Germany arturoquintana.github.io/germany-dayahead-ledger")
    return subject, "\n".join(lines)


def email_digest(summary: dict, *, _smtp=smtplib.SMTP) -> None:
    """Send the digest after ticks that settled something, or on trouble
    (fetch failure / primary receipt missing). Config in .env: ALERT_EMAIL_TO
    + ALERT_SMTP_PASSWORD (Gmail app password; ALERT_SMTP_USER defaults to
    the recipient). Port 587 + STARTTLS deliberately: Hetzner blocks
    outbound 465/25 by default. No config -> silently off. Never fatal — a
    MISSING daily email is itself a dead-man signal, like the heartbeat."""
    to = _env("ALERT_EMAIL_TO")
    password = _env("ALERT_SMTP_PASSWORD")
    if not to or not password:
        return
    trouble = bool(summary.get("fetch_error")) or \
        not summary.get("primary_receipt_stands", True)
    if not summary.get("settled") and not trouble:
        return                      # commit-only passes stay silent
    try:
        built = build_digest()
        if built is None:
            return
        subject, body = built
        if trouble:
            problems = []
            if summary.get("fetch_error"):
                problems.append(f"fetch_error: {summary['fetch_error']}")
            if not summary.get("primary_receipt_stands", True):
                problems.append("PRIMARY RECEIPT DOES NOT STAND for tomorrow")
            body = "TICK TROUBLE: " + "; ".join(problems) + "\n\n" + body
            if not subject.startswith("ALERT"):
                subject = "ALERT — " + subject
        msg = EmailMessage()
        msg["From"] = _env("ALERT_SMTP_USER") or to
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with _smtp("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(_env("ALERT_SMTP_USER") or to, password)
            s.send_message(msg)
        print(f"[esios-paper] digest emailed ({subject!r})")
    except Exception as exc:
        print(f"[esios-paper] digest email FAILED (non-fatal): {exc}")


def ots_manifest(label: str, receipts: Path | None = None,
                 ledger: Path | None = None) -> str:
    """Text manifest binding one market's audit trail: sha256 of its receipts
    and ledger files. Stamping this (OpenTimestamps) proves the receipts
    existed BEFORE the auction published — trustlessly, unlike git host
    timestamps. Pure text builder, unit-tested. Defaults to the ES globals
    (resolved at call time so tests can monkeypatch them)."""
    receipts, ledger = receipts or RECEIPTS, ledger or LEDGER
    lines = [f"esios-paper audit manifest {label}"]
    for path in (receipts, ledger):
        digest = (hashlib.sha256(path.read_bytes()).hexdigest()
                  if path.exists() else "absent")
        lines.append(f"sha256({path.name})={digest}")
    return "\n".join(lines) + "\n"


def ots_stamp(label: str, *, ots_dir: Path | None = None,
              receipts: Path | None = None, ledger: Path | None = None,
              _run=subprocess.run) -> None:
    """Write <ots_dir>/<label>.txt and stamp it via the canonical OTS client
    (through uvx, so the project stays stdlib-only). The .txt + .ots proof
    ride the same git backup as the data they attest. Best effort: a
    calendar/network failure must never fail the tick. Per-market: ES uses
    the defaults; silent markets pass their own Data/<slug>/ots + files
    (defaults resolved at call time so tests can monkeypatch)."""
    ots_dir = ots_dir or OTS_DIR
    receipts, ledger = receipts or RECEIPTS, ledger or LEDGER
    try:
        ots_dir.mkdir(parents=True, exist_ok=True)
        content = ots_manifest(label, receipts, ledger)
        # A STAMPED MANIFEST IS IMMUTABLE. The pre-2026-08-21 version
        # rewrote <date>.txt on every pass and skipped stamping when a proof
        # existed — so the second tick of a day (post-settlement hashes)
        # silently invalidated the morning's proof (audit finding, incident
        # 2026-08-21). Now: if the audit trail moved after a stamp, the new
        # state gets a NEW suffixed manifest; stamped pairs are never touched.
        for n in range(1, 25):
            manifest = ots_dir / (f"{label}.txt" if n == 1
                                  else f"{label}-{n}.txt")
            proof = manifest.with_suffix(".txt.ots")
            if proof.exists():
                if manifest.read_text() == content:
                    print(f"[esios-paper] ots: {manifest.name} already "
                          "attested for current state")
                    return
                continue            # stamped for an older state; next slot
            manifest.write_text(content)
            r = _run(["uvx", "--from", OTS_CLIENT, "ots",
                      "stamp", str(manifest)],
                     capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and proof.exists():
                print(f"[esios-paper] ots: stamped {manifest.name}")
            else:
                print(f"[esios-paper] ots: STAMP-FAILED "
                      f"{(r.stderr or r.stdout).strip()[:200]}")
            return
        print("[esios-paper] ots: STAMP-FAILED (slot overflow)")
    except Exception as exc:
        print(f"[esios-paper] ots: STAMP-FAILED {exc}")


def git_backup(label: str) -> None:
    """Commit + push Data/ (prices, receipts, ledger — the audit trail). Best
    effort by design: a failed commit/push must never fail the tick (the next
    day's push carries today's commit), but the outcome is always logged."""
    def run(*args: str):
        return subprocess.run(["git", "-C", str(REPO), *args],
                              capture_output=True, text=True)
    run("add", "Data")
    if run("diff", "--cached", "--quiet").returncode == 0:
        print("[esios-paper] backup: up to date")
        return
    c = run("commit", "-m", f"data: tick {label}")
    if c.returncode != 0:
        print(f"[esios-paper] backup: COMMIT-FAILED {c.stderr.strip()[:200]}")
        return
    p = run("push")
    print("[esios-paper] backup: pushed" if p.returncode == 0
          else f"[esios-paper] backup: committed, PUSH-FAILED (will ride next "
               f"push) {p.stderr.strip()[:200]}")


def _print_summary(s: dict, tag: str) -> None:
    if s.get("fetch_error"):
        print(f"[{tag}] FETCH-ERROR {s['fetch_error']}")
    for e in s["settled"]:
        print(f"[{tag}] SETTLED {e['target']} [{e['strategy']} "
              f"v{e['strategy_version']}]: pnl={e['pnl_eur']:+.2f} "
              f"(oracle {e['oracle_pnl_eur']:+.2f}, capture {e['capture']}, "
              f"tau {e.get('tau')})")
    for r in s["committed"]:
        print(f"[{tag}] COMMITTED {r['target']} [{r['strategy']} "
              f"v{r['strategy_version']}]: buy {r['buy_hours']} "
              f"sell {r['sell_hours']} (basis {r['basis_day']})")
    for msg in s["skipped"]:
        print(f"[{tag}] SKIPPED commit: {msg}")


def cmd_tick(market_slug: str | None = None) -> int:
    """ES (default): full pass — settle/commit + OTS + git push + heartbeat +
    email. A non-ES market runs a SILENT pass: settle/commit into its own
    Data/<slug>/ and print, but NO heartbeat/email/OTS and NO separate git
    push — its files ride the ES tick's git_backup (server_tick.sh runs the
    silent markets first). Keeps the ES heartbeat/digest ES-only."""
    if market_slug and market_slug != "es":
        from .markets import MARKETS
        if market_slug not in MARKETS:
            print(f"unknown market {market_slug!r}; use: "
                  f"{', '.join(MARKETS)}")
            return 2
        m = MARKETS[market_slug]
        try:
            with writer_lock(m.ledger_path.parent / ".tick.lock"):
                s = tick(market=m)
                _print_summary(s, f"esios-paper:{market_slug}")
                # OTS-anchor this market's audit trail into its own Data/<slug>/
                # ots (rides the ES tick's git_backup). No heartbeat/email/push.
                ots_stamp(s["date"], ots_dir=m.ledger_path.parent / "ots",
                          receipts=m.receipts_path, ledger=m.ledger_path)
        except WriterLockError as exc:
            print(f"[esios-paper:{market_slug}] {exc}")
            return 3
        print(f"[esios-paper:{market_slug}] tick done "
              f"{json.dumps({k: v if isinstance(v, (str, bool)) else bool(v) for k, v in s.items()})}")
        return 0

    try:
        with writer_lock(DATA_DIR / "es" / ".tick.lock"):
            s = tick()
            _print_summary(s, "esios-paper")
            ots_stamp(s["date"])
            git_backup(s["date"])
            heartbeat(s["primary_receipt_stands"])
            email_digest(s)
    except WriterLockError as exc:
        print(f"[esios-paper] {exc}")
        return 3
    print(f"[esios-paper] tick done {json.dumps({k: v if isinstance(v, (str, bool)) else bool(v) for k, v in s.items()})}")
    return 0


def cmd_status() -> int:
    alerts = DATA_DIR / "CROSSCHECK-ALERTS.log"
    if alerts.exists():
        print("!! CROSSCHECK ALERTS present — the two price routes disagreed; "
              "investigate before trusting recent settlements "
              f"({alerts.name}, {len(alerts.read_text().splitlines())} entries)")
    ledger = _load_jsonl(LEDGER)
    receipts = _load_jsonl(RECEIPTS)
    settled_keys = {(e["target"], e["strategy"], e["strategy_version"]) for e in ledger}
    open_receipts = [r for r in receipts
                     if (r["target"], r["strategy"], r["strategy_version"])
                     not in settled_keys]
    print(f"receipts: {len(receipts)} total, {len(open_receipts)} open")
    if not ledger:
        print("ledger: empty (no settled days yet)")
        return 0
    by_strategy: dict[tuple[str, str], list[dict]] = {}
    for e in ledger:
        by_strategy.setdefault((e["strategy"], e["strategy_version"]), []).append(e)
    for (strat, ver), es in sorted(by_strategy.items()):
        pnl = [e["pnl_eur"] for e in es]
        caps = [e["capture"] for e in es if e.get("capture") is not None]
        cap_s = f"{statistics.fmean(caps) * 100:.0f}%" if caps else "n/a"
        print(f"  {strat} v{ver}: {len(pnl)} settled | total {sum(pnl):+.2f} EUR | "
              f"mean {statistics.fmean(pnl):+.2f}/day | "
              f"win rate {sum(p > 0 for p in pnl) / len(pnl) * 100:.0f}% | "
              f"mean capture {cap_s}")
    return 0


def cmd_markets() -> int:
    """Emit market slugs (space-separated) for shell consumers so no script
    hardcodes the set: `markets` = all; `--driver server|actions`; `--public`."""
    from .markets import MARKETS, by_driver, public_markets
    args = sys.argv[1:]
    if "--driver" in args:
        sel = by_driver(args[args.index("--driver") + 1])
    elif "--public" in args:
        sel = public_markets()
    else:
        sel = list(MARKETS.values())
    print(" ".join(m.slug for m in sel))
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "tick"
    market = None
    if "--market" in args:
        i = args.index("--market")
        market = args[i + 1] if i + 1 < len(args) else None
    if cmd == "tick":
        return cmd_tick(market)
    if cmd == "status":
        return cmd_status()
    if cmd == "markets":
        return cmd_markets()
    print(f"unknown command {cmd!r}; use: tick [--market <slug>] | status | "
          "markets [--driver <d>|--public]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
