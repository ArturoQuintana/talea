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
from pathlib import Path
from types import SimpleNamespace

from .liveness import assess, describe
from .loop import (DATA_DIR, LEDGER, RECEIPTS, STRATEGY, WriterLockError,
                   _load_jsonl, tick, writer_lock)

REPO = Path(__file__).resolve().parents[2]
# The comparison rules as amended (docs/multiple-comparisons-policy.md,
# Amendment 1, ratified 2026-11-10). The digest only COUNTS toward them; it
# never evaluates or declares anything.
AMENDMENT_1_AT = "2026-11-11T03:30:00"          # UTC; rows settled later count
CONFIRMATORY_SHADOW = "battery-2h2h-climatology"  # vs the primary, in ES
REGISTER = REPO / "docs" / "open-decisions.md"
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
        f"{statistics.fmean(caps) * 100:.1f}%")

    by_day: dict[str, dict[str, dict]] = {}
    for e in ledger:
        by_day.setdefault(e["target"], {})[e["strategy"]] = e
    for shadow in sorted({e["strategy"] for e in ledger} - {STRATEGY}):
        pairs = [(v[shadow], v[STRATEGY]) for _t, v in sorted(by_day.items())
                 if shadow in v and STRATEGY in v]
        # COUNTS ONLY — wins, ties and losses, ties per rule A (round-to-cents).
        # No p-value and no verdict: under Amendment 1 a comparison is decided
        # only at a month-end review, and the digest printing a p-value every
        # day was itself a daily look (it showed rankblend below 0.05 from
        # 2026-09-14, at 21 non-tied days). What the reader needs daily is how
        # far each comparison is from its decision look.
        def wtl(ps):
            ds = [round(s["pnl_eur"] - p["pnl_eur"], 2) for s, p in ps]
            w, l = sum(d >= 0.01 for d in ds), sum(d <= -0.01 for d in ds)
            return w, len(ds) - w - l, l
        w, t, l = wtl(pairs)
        total = sum(s["pnl_eur"] - p["pnl_eur"] for s, p in pairs)
        if shadow == CONFIRMATORY_SHADOW:
            progress = (f"confirmatory: {min(w + l, 90)} of 90 non-tied days toward "
                        f"its one terminal look")
        else:
            since = [(s, p) for s, p in pairs
                     if min(s.get("settled_at", ""), p.get("settled_at", "")) > AMENDMENT_1_AT]
            w2, t2, l2 = wtl(since)
            progress = (f"since the rules were amended {w2}W-{t2}T-{l2}L, "
                        f"{w2 + l2} of 30 non-tied days toward a decision look")
        lines.append(
            f"{shadow} vs primary: {total:+.2f} EUR over {len(pairs)} shared "
            f"({w}W-{t}T-{l}L); {progress} — decided only at a month-end review")
    waiting = _open_decisions((now or datetime.now(timezone.utc)).date())
    if waiting:
        lines.append("")
        lines.append(waiting)

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
        tag = f"{mk.slug.upper()} {'public' if mk.public else 'silent'}"
        mp = [e for e in mrows if e["strategy"] == STRATEGY]
        if not mp:
            # A market whose primary never commits (GB, by the nature of its
            # feed) used to be skipped here, so a PUBLIC market was absent from
            # every digest. Show what does commit.
            parts = []
            for name in sorted({e["strategy"] for e in mrows}):
                rows = [e for e in mrows if e["strategy"] == name]
                parts.append(f"{name.rsplit('-', 1)[-1]} {len(rows)} days "
                             f"{sum(e['pnl_eur'] for e in rows):+.2f}")
            lines.append("")
            lines.append(f"[{tag}] primary does not commit here | " + " | ".join(parts)
                         + f" | latest {max(e['target'] for e in mrows)}")
            continue
        last_t = max(e["target"] for e in mp)
        le = next(e for e in mp if e["target"] == last_t)
        mcaps = [e["capture"] for e in mp if e.get("capture") is not None]
        lines.append("")
        lines.append(
            f"[{tag}] {len(mp)} days | total {sum(e['pnl_eur'] for e in mp):+.2f} | "
            f"mean capture {statistics.fmean(mcaps) * 100:.1f}% | "
            f"latest {last_t}: {le['pnl_eur']:+.2f} (cap {le.get('capture')})")
    lines.append("")
    lines.append("Paper money, upper bound (exchange fees only).")
    lines.append("Dashboards: arturoquintana.github.io/talea")
    return subject, "\n".join(lines)


def _open_decisions(today) -> str | None:
    """One line for the digest: how many operator decisions are open in the
    register, and how old the oldest is. Requests of the operator used to live
    only in report emails and about half went unanswered (incident 2026-11-10);
    this puts the count in front of the reader every day. None if there is no
    register or nothing is open."""
    from datetime import date
    if not REGISTER.exists():
        return None
    rows = []
    for line in REGISTER.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].startswith("D-") and cells[2] == "OPEN":
            rows.append((date.fromisoformat(cells[1]), cells[0]))
    if not rows:
        return None
    raised, rid = min(rows)
    return (f"Waiting on you: {len(rows)} open decision{'s' if len(rows) != 1 else ''} "
            f"(oldest {rid}, {(today - raised).days} days) — docs/open-decisions.md")


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
        sender = _env("ALERT_SMTP_USER") or to
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        if sender != to:
            # the alert account keeps a copy in its own inbox: it is the one
            # mailbox the project's agents can read
            msg["Cc"] = sender
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
    if p.returncode != 0:
        # Self-heal a REJECTED push (non-fast-forward): other writers (ERCOT/JP
        # GitHub Actions) push Data/<their slug>/ to the same main between our
        # pull and our push. A bare retry-next-tick never recovers — the next
        # pre-tick `pull --ff-only` fails on the now-diverged history, so every
        # later commit stays local forever (incident 2026-11-03..09: ES/DE/GB
        # never reached origin and ERCOT never reached the mirror). Rebase our
        # UNPUSHED local commits onto origin (writers own disjoint Data/<slug>/
        # paths, so this is conflict-free; on any conflict we abort and leave
        # the local state intact) and push once more. Never a force-push: no
        # published history is rewritten. No --autostash: on a dirty tree it can
        # exit 0 with conflict markers left in a file; without it a dirty tree
        # makes the rebase refuse and the failure is logged below.
        r = run("pull", "--rebase")
        if r.returncode != 0:
            run("rebase", "--abort")
        else:
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
