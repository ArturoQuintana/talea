# Governance (public extract)

The full operating constitution lives in the private operations repo; this
extract contains every part an outside verifier needs. Nothing here may be
weakened by the operators; changes require the owner's written amendment,
before the data that would trigger them.

## Invariants

1. **Leak guard**: a receipt for target day T is committed only if no price
   for T exists in the project's dataset. A missed day is honest; a leaked
   receipt is worthless. Never bypassed, never backfilled.
2. **Append-only audit trail**: receipts and ledger are never edited,
   revised, or regenerated.
3. **Pre-registered strategies**: changing decision logic means a NEW
   strategy id/version on new receipts; old receipts stand forever
   (graveyard rule — retired strategies keep their settled history).
4. **Explicit costs**: P&L includes round-trip efficiency and fees; gross
   is never reported as net.

## Falsification (pre-registered stop conditions)

1. ≥90 settled days AND primary net P&L ≤ 0 → the premise is falsified;
   wind down or redesign openly.
2. Rolling 30-day mean capture < 70% for EVERY panel strategy → freeze all
   escalation; diagnose the regime change first.
3. If candidate models cannot beat the best pre-registered baseline BOTH
   out-of-sample (≥1-year temporal backtest) AND on ≥30 shared live shadow
   days → "our forecast adds sellable value" is falsified.

Scope (Amendment 1, below): these three are criteria of the project's premise
and apply to Spain, the primary market. In any other market, condition 2 has
that market's own consequence: no new strategy or escalation there until the
cause is diagnosed and written up.

## Deciding strategy comparisons

"A beats B" is decided by a one-sided sign test on paired daily P&L deltas
over SHARED settled days. Deltas are rounded to cents; |Δ| < 0.01 is a tie and
is excluded. Below the bar, no superiority claims — anywhere.

**The p-value** (ratified by the owner 2026-08-27, before any comparison had
30 days). `p_eff = max(p_iid, p_boot)`: the plain sign-test p, floored against
a moving-block-bootstrap p that allows for days not being independent. `p_boot`
is read from a table frozen on 2026-08-27 and published with the data,
`Data/calibration/sign_bar.json` (SHA-256
`024cca63c588211f5547d81e7d9ae27f2753e9dec4135ec1729b4bb68dd04044`). It covers
30 to 90 non-tied days; beyond 90 the plain sign test applies. Taking the
larger of the two means the table can only make the bar harder.

**Amendment 1** (ratified by the owner in writing 2026-11-10; in force for data
settled after 2026-11-11T03:30:00Z). It fixes when a comparison is decided and
what it is corrected for:

1. **One confirmatory comparison:** climatology vs persistence in Spain,
   registered 2026-08-01. It is decided once, on exactly its first 90 non-tied
   shared days since 2026-08-01, at 0.05. The verdict is final and is published
   either way.
2. **Every other comparison is exploratory.** It counts only days settled after
   the amendment, needs at least 30 non-tied days, and is decided only at a
   month-end review, at a threshold of 0.05/12 per review, Holm-corrected
   within its market. A market's family is each "shadow beats the primary" and
   each comparison a retirement needs, where its precondition is met.
3. **Between reviews** tools and digests show wins, ties and losses. They show
   no p-value and no verdict.
4. **Promotion** of a shadow to primary also needs its pooled capture (sum of
   P&L over sum of oracle P&L, over the comparison's days) to be at least the
   primary's. **Retirement** of a shadow needs it to lose both to the primary
   and to another shadow, each on at least 60 shared and 30 non-tied days.
   Both run per market, against that market's own primary; a market whose
   primary does not commit receipts has no evaluation.
5. **Forward only.** Nothing settled before the amendment decides an
   exploratory comparison, whatever it shows.

**Reporting note for the confirmatory comparison** (written 2026-11-11, when it
stood at 83 of its 90 non-tied days; in force from the commit that published
it). It changes no rule: the verdict is decided on the booked P&L as registered.
The published verdict will also give, labelled "not pre-registered — decides
nothing", wins, ties and losses for the same 90 days on the executable P&L
(VERIFY.md section 4, daily-reset reading), and no claim will be made from
them. If the bar is not met, persistence stays the primary in Spain and the
owner will not promote climatology by discretion: it can become primary only
under a rule pre-registered afterwards and decided on days settled after that
rule. If the bar is met, promotion follows rule 4 with no discretion.

Every verdict is recomputed by the statistical referee (below) before it is
acted on or published. To reproduce: `python scripts/compare_strategies.py`
prints the counts; with `--decision-look` it prints `p_eff`, each market's
family and the thresholds, as a month-end review sees them.

## Independent verification roles

- A weekly fresh-context AUDITOR (read-only) extracts the public claims and
  verifies them against observable reality.
- A STATISTICAL REFEREE (fresh-context, read-only) must independently
  recompute every published statistical claim from the pre-registration
  documents and raw data before publication; a discrepancy blocks
  publication. The referee is barred from proposing method changes.
- The CLOSED DEFECT LOOP: every incident or audit finding must end as a
  detector or regression test for its class; the tracked metric is the
  escape rate (failures noticed by humans before machinery).
