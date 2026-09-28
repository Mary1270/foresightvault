![License](https://img.shields.io/badge/license-MIT-blue)
![GenLayer](https://img.shields.io/badge/GenLayer-Intelligent%20Contract-3E9B4F)
![Tests](https://img.shields.io/badge/offline%20tests-150%20passing-3E9B4F)

# ForesightVault

A multi-outcome, multi-participant **prediction pool for real-world events**
(elections, rulings, launches) on GenLayer. Anyone can stake GEN on one of
2–6 fixed outcomes. After the event, anyone can trigger settlement: every
validator independently fetches the submitted news sources, and the market
settles only if they reach **exactly the same decision**, backed by at
least two independent, confirmed sources that **quote the outcome
verbatim**. Winners then claim the whole pot in proportion to their stake.

The trust pattern (address-bound stakes, a reputable-domain allowlist, a
quality-gated locked source set, pull payments) comes from an earlier,
reviewed two-party sports-settlement contract. What is new here: N-way
outcomes instead of over/under, a pooled market with proportional payouts
instead of a two-party bet, grounded (quoted) and confirmed-only evidence,
exact-decision validator consensus, and O(1) per-user claims.

---

## Repository layout

```
contract.py               documented source of truth (GenLayer / GenVM, Python)
contract_deploy.py        the file to deploy: contract.py with comments and
                          docstrings removed, verified AST-identical
tools/build_deploy.py     builds and verifies contract_deploy.py
index.html                the whole frontend (no build step)
tests/                    offline test suite + minimal genlayer SDK stub
.github/workflows/        CI: runs every test against BOTH contract files
```

Run the tests (no installation needed):

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
FORESIGHTVAULT_CONTRACT=contract_deploy.py python3 -m unittest discover -s tests -p "test_*.py"
python3 tools/build_deploy.py --check
```

**Why two contract files?** GenLayer Studio's schema step can fail with a
generic `invalid_contract` error on files with a large volume of comments.
`contract_deploy.py` is generated from `contract.py` by
`tools/build_deploy.py`, which locates docstrings with `ast` and comments
with `tokenize` (never regex), keeps the runner header verbatim, and
refuses to write the file unless its syntax tree is identical to the
original's minus docstrings. CI fails if the two ever drift apart.

---

## Lifecycle

```
create_market ──► stake (anyone, many times) ──► staking_deadline
                                                      │
                                   resolution_deadline ▼ (48h window)
           resolve_market (anyone) ── decisive ──► resolved ──► claim ──► withdraw
                 │   └─ winner nobody backed ──► refunding ─┘
                 └─ indeterminate: retry within window
           window closes undecided ── expire_market ──► refunding ──► claim ──► withdraw
```

| Method | Who | What it does |
|---|---|---|
| `create_market(question, outcomes, required_source_domains, staking_deadline, resolution_deadline)` | anyone | Defines the question, 2–6 outcome labels, ≥2 committed news domains (optionally `domain/path`), and timing. Not payable. |
| `stake(market_id, outcome_index)` *payable* | anyone | Adds the attached GEN to a position. Repeat stakes add up; hedging across outcomes is allowed. An invalid stake does **not** revert: the attached GEN is credited back to the sender (see [Live verification](#live-verification)). |
| `resolve_market(market_id, source_urls)` | anyone | Runs the consensus pipeline below. |
| `expire_market(market_id)` | anyone | After the window closes undecided, switches the market to refunds. |
| `claim(market_id)` | each staker | Credits the caller's payout or refund. Once per address per market. |
| `withdraw()` | anyone | Sends the caller's credited balance to their wallet. The only method that moves GEN out. |

Views: `get_market`, `list_markets(offset, limit)` (newest first, max 50
per page), `get_stake`, `get_claimable`, `has_claimed`,
`get_pending_withdrawal`, `get_contract_balance`, `get_accounting`,
`total_markets`.

Timing bounds: staking closes 30 minutes to 30 days after creation;
resolution opens 5 minutes to 30 days after staking closes; the resolution
window lasts 48 hours.

---

## How settlement reaches consensus

1. **Validated before any fetch.** `resolve_market` rejects the call unless
   there are 2–6 URLs, every URL is a strictly valid https URL on the
   allowlist, at most one URL per domain, and every committed domain (and
   committed path prefix) is covered.
2. **Per-source evaluation.** Each page is fetched with
   `gl.nondet.web.render` and the model answers four fixed-format
   questions: `RELEVANCE`, `STATUS` (Confirmed / Projected / Unknown),
   `OUTCOME` (an outcome label copied verbatim) and `QUOTE` (a verbatim
   excerpt stating the outcome). The contract, not the model, decides
   whether a source counts. It must be Relevant, Confirmed, name exactly one
   listed outcome, and its quote must actually appear in the fetched page.
3. **Quorum.** A decisive result needs ≥2 eligible sources and a strict
   plurality (no tie for first place).
4. **Exact-decision consensus.** Settlement uses
   `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`. Every validator
   re-fetches and re-evaluates every source itself, and accepts the
   leader's result only if its own **decision** is identical: the same
   winning outcome (or none), and the same answer to "is this evidence
   strong enough to lock the source set". Per-source audit details
   (quotes, fetch statuses) are excluded from the comparison, because
   independently fetched pages can legitimately differ byte-for-byte while
   supporting the same decision. The validator also rejects anything that
   is not a successful `gl.vm.Return`, and any malformed leader payload.
5. **If validators disagree,** the transaction is not accepted and
   nothing changes. The market stays open for another attempt within the
   window, and becomes refundable via `expire_market` afterwards. Funds
   can never be stuck by a failed consensus.

The evaluation closures capture only plain values, never `self`, and every
exception inside them (fetch failures, model errors) is caught and turned
into a record with a non-`ok` quality flag. An exception escaping a
non-deterministic block cannot be caught by the calling contract code.

---

## Money model

- **Escrow.** All stakes are held by the contract (`get_contract_balance`
  reads the real on-chain balance).
- **Resolved:** `payout = total_pool × my_stake_on_winner ÷ winning_pool`
  (floor). The last winning staker to claim also receives the rounding
  remainder, so winners are paid exactly the whole pot. Losing stakes are
  not returned; this is a real pooled market.
- **Refunding** (window expired undecided, or the confirmed outcome had no
  stakers): every staker claims back exactly what they staked, across all
  outcomes.
- **Claims are per user and O(1).** Positions live in their own storage
  map, keyed by market, outcome and address. No code path loops over
  stakers, so there is no participant cap and no way to make settlement
  more expensive or to block other users.
- **Pull payments.** `claim` only credits a balance. `withdraw` zeroes the
  balance *before* transferring (checks-effects-interactions), so one
  wallet that fails to receive funds can never block anyone else.
- **Refund instead of revert.** On GenLayer, GEN attached to a payable call
  that reverts still lands in the contract, while the revert erases any
  record of the sender (verified live, see below). So once value is
  attached, `stake` never reverts: an invalid stake (unknown market,
  closed staking, bad outcome index, below the minimum) credits the full
  amount back to the sender's withdrawable balance and returns
  `{"accepted": false, "reason": ..., "refunded_wei": ...}`. Only a
  zero-value call is rejected with an error.
- **Transparent accounting.** `get_accounting` reports the real balance,
  the contract's liabilities (every wei accepted through `stake` and not
  yet withdrawn), and any unaccounted surplus, so solvency can be audited
  on-chain at any time.

A solvency test stakes into two markets from 25 wallets, resolves one,
refunds the other, claims and withdraws everything, and checks that GEN
paid out equals GEN taken in, to the wei. A seeded fuzz test repeats this
over 300 random markets (random stakers, amounts, hedging, winner, claim
order, and resolved/refunded endings).

---

## Threat model

| Threat | Mitigation |
|---|---|
| URL made to *look* allowlisted but fetched from elsewhere (`https://evil.example\@reuters.com`, `https://reuters.com@evil.example`) | Strict parser: https only, no credentials, no backslashes/whitespace/control characters, port 443 only, no IP hosts, validated hostname labels. The host checked is the host fetched. |
| Escaping a committed site section (`reuters.com/world/../sport`, `%2e%2e`) | Dot segments, encoded dots and encoded separators are rejected. Prefix matching is segment-aware (`/world` never matches `/worldcup`). |
| One hostile or wrong page decides the market | ≥2 independent allowlisted domains must agree, with a strict plurality. At most one URL per domain. |
| Model hallucinates or is prompt-injected by a page | All inputs are declared untrusted data. The outcome must be quoted **verbatim from the fetched page** (checked by the contract). Every validator independently repeats the whole evaluation. |
| Polls, projections, or "expected to win" articles settle early | `STATUS` must be `Confirmed`; anything else is ineligible. |
| Leader reports a result validators can't reproduce, or a malformed/type-confused one (e.g. `true` where the index `1` belongs, since `True == 1` in Python) | Validators compare the exact decision with strict types (bools are never accepted as integers) and reject any malformed payload; any mismatch rejects the transaction. |
| Resolver shops for a favourable source combination | Once an attempt produces real evidence (≥2 eligible sources, even if split), the exact URL set (case-sensitive, order-independent) is locked for all retries. Irrelevant first attempts don't lock, so they can't poison a market. |
| Resolving before the event | `resolve_market` only opens at `resolution_deadline`, after staking has closed. |
| Ambiguous outcome labels (`Yes` vs `Yes.`, or a label named `None`) | Rejected at creation, using the same normalization as answer parsing. The first labeled `OUTCOME:` line is authoritative; mentions elsewhere are ignored. |
| Spam stakes to block users or inflate settlement cost | No loops and no caps: per-user O(1) claims. The anti-dust minimum stake is 0.001 GEN. |
| Unbounded reads | `list_markets` is paginated (max 50 per page). |
| Funds trapped | Every terminal state has a claim path. Rounding remainder is paid out. Failed consensus changes nothing. Invalid stakes are refunded instead of reverted, because a reverted payable call on GenLayer keeps the GEN with no record of the sender. |

### Known limitations (disclosed)

- **Market design is the creator's responsibility.** The contract cannot
  know when an event's outcome becomes public. If the creator sets
  `staking_deadline` after that point, late stakers could bet on a known
  result. The UI warns about this at creation.
- **The locked source set is final.** If a locked page later goes offline,
  the market cannot resolve and will be refunded via `expire_market`.
- **Allowlist scope.** Evidence comes only from the fixed allowlist of
  major news outlets. Pages must be server-rendered; JavaScript-only pages
  cannot be read by the fetcher.
- **Exact label matching.** A model that paraphrases a correct outcome is
  not credited for that source (it can only make a market indeterminate,
  never pick a wrong winner).
- **Stakes are final.** There is no un-staking before the deadline;
  undecided markets refund in full.
- **Only the latest attempt's evidence is stored.** Earlier inconclusive
  attempts are overwritten. Retries after a lock always use the same
  sources.

---

## Frontend

`index.html` is a single, dependency-free file (`genlayer-js` from esm.sh)
with real hash routing:

- `#/` **Explore**: paginated list of all markets (newest first, "Load
  more"), with a client-side filter and a status badge per market.
- `#/create` **Create market**: dynamic outcome rows, source domains, and
  deadlines. On success it opens the new market's page.
- `#/market/<id>` **Market page**: status (staking open / staking closed /
  awaiting resolution / expirable / resolved / refunding), outcome pools
  with staker counts, the evidence trail including each source's quoted
  evidence, the connected wallet's position with **Claim** and **Withdraw**
  buttons, and Stake / Resolve / Expire actions.
- `#/withdraw` **Balances**: withdraw, look up any address's stake, and
  check the contract balance.

Every write waits for the transaction to finalize before reporting a
result. All dynamic content is rendered with `textContent` and
`createElement` only (`innerHTML` is never used). GEN amounts use `BigInt`
throughout.

---

## Live verification

### v1.0 (superseded test deployment): [`0x578c7449B5D7DC6509730E38389A11503D9b735a`](https://explorer-studio.genlayer.com/address/0x578c7449B5D7DC6509730E38389A11503D9b735a)

A full lifecycle was run with real GEN on GenLayer Studio, using a real
event (the 2026 US Open men's singles final) and two wallets:

| Step | Result |
|---|---|
| `create_market` (2 outcomes, `cnn.com` + `bbc.com` committed) | Accepted, 0 rotations |
| Stakes: 1 GEN on Zverev (wallet A), 3 GEN on Shelton (wallet B) | Pools and staker counts correct; contract balance 4 GEN |
| Guards: zero stake, early resolve, early claim, early expire, empty withdraw, out-of-range index, reserved label `None`, labels `Yes`/`Yes.`, stake after deadline | All rejected with the expected messages |
| `resolve_market` with a host-confusion URL (`https://attacker.example\@cnn.com/x`) | Rejected before any fetch |
| `resolve_market` with CNN + BBC | CNN returned no usable text to the fetcher; BBC was confirmed with a verbatim quote. Only 1 eligible source, so the result was `Indeterminate` and the source set was **not** locked (by design) |
| `resolve_market` with CNN + BBC + Guardian | **Resolved: Alexander Zverev.** 2 independent confirmed sources, each with a verbatim quote; validators agreed with 0 rotations and the transaction finalized. The BBC quote differed from the first attempt, and validators still agreed, as designed: they compare the decision, not the audit text |
| `claim` by the winner | Credited exactly 4 GEN (the whole pot) |
| Second `claim`, and `claim` by the losing wallet | Both rejected |
| `withdraw` | 4 GEN transferred to wallet A |

**Finding from this run.** After everything was withdrawn, the contract
still held 2 GEN: exactly the two 1-GEN stakes that had been rejected
(out-of-range index, and staking closed). On GenLayer, a payable call that
reverts keeps the attached GEN in the contract, but the revert discards any
record of the sender, so that GEN was unrecoverable. v1.1 fixes this:
invalid stakes are refunded to the sender instead of reverting, and
`get_accounting` makes any such surplus visible. The offline test stub was
changed to model this real behavior, and regression tests, mutation checks
and the fuzz test now cover it. The 2 GEN on the v1.0 deployment were test
funds.

**Practical note.** `cnn.com` article pages returned no usable text to the
GenLayer fetcher in this run, while `bbc.com` and `theguardian.com` worked.
Market creators should commit domains whose article pages are
server-rendered.

### v1.1 (current): [`0xe02e66A77c9E5b177F187C9C25915fa49cef9013`](https://explorer-studio.genlayer.com/address/0xe02e66A77c9E5b177F187C9C25915fa49cef9013)

The refund fix and the accounting view were re-verified live:

| Step | Result |
|---|---|
| `get_accounting` on a fresh deployment | All values 0 |
| `create_market` (`bbc.com` + `theguardian.com` committed) | Accepted |
| `stake` 1 GEN with `outcome_index` 9 | **SUCCESS** (no revert): `{"accepted": false, "reason": "outcome_index must be an integer between 0 and 1.", "refunded_wei": "1000000000000000000"}` |
| `get_pending_withdrawal` for that wallet | 1 GEN |
| `withdraw` | 1 GEN returned to the sender |
| `stake` 1 GEN after the staking deadline (the exact case that lost GEN on v1.0) | **SUCCESS** (no revert): `accepted: false`, "Staking for this market has closed.", 1 GEN refunded |
| `withdraw` | 1 GEN returned to the sender |
| `resolve_market` with BBC + Guardian on a market nobody staked on | Resolved to Alexander Zverev from 2 quoted, confirmed sources (0 rotations, finalized); status `refunding`, reason `no_winning_stakers` |
| `get_accounting` at the end | Balance 0, liabilities 0, unaccounted surplus 0; received 2 GEN, withdrawn 2 GEN |

`claim` and `withdraw` for a winning position were verified live on v1.0;
their code is unchanged in v1.1.

**Frontend, against the live v1.1 contract** (GitHub Pages): the Explore
list, market page (status, winner, locked sources, and the evidence trail
showing each source's verbatim quote), and the accounting panel all read
correctly from chain. A write through the site itself (`create_market`
from an in-browser test session) was sent, waited on until finalized, and
navigated automatically to the new market (#1, a frontend test market).
Live testing also caught one display bug, fixed: the Explore list labeled a
refunding market's reason as "expired" because list summaries carry no
refund reason; it now shows the reason only when the full record has it.

## Deploying

1. Deploy **`contract_deploy.py`** on GenLayer Studio (no constructor
   arguments).
2. Put the deployed address into `CONTRACT_ADDRESS` in `index.html`.
3. Push to GitHub and enable GitHub Pages for the frontend.

## Live verification walkthrough (for reviewers)

This path exercises escrow, consensus, payout, and withdrawal with real
GEN, using two wallets (A and B):

1. **Create:** `create_market` with outcomes such as `["Yes", "No"]`,
   `required_source_domains` of two allowlisted domains that have already
   published a confirmed story (so resolution can be tested), a
   `staking_deadline` ~35 minutes out and a `resolution_deadline` 5
   minutes after that.
2. **Stake:** wallet A stakes 1 GEN on the true outcome; wallet B stakes 3
   GEN on the other. `get_contract_balance` shows 4 GEN.
3. **Guards:** `stake` with 0 value, and `resolve_market` before the
   window, both revert.
4. **Resolve:** after `resolution_deadline`, call `resolve_market` with one
   article URL per committed domain. The market becomes `resolved`, and the
   evidence trail shows each source's verbatim quote.
5. **Claim:** A's `get_claimable` shows 4 GEN; A calls `claim`, then
   `withdraw`, and receives 4 GEN in the wallet. B's `claim` reverts ("no
   stake on the winning outcome"). A second `claim` by A reverts.
6. **Refund check:** wallet B stakes 1 GEN with `outcome_index` 9. The
   call succeeds with `{"accepted": false, ...}`; `get_pending_withdrawal`
   for B shows 1 GEN and `withdraw` returns it.
7. **Accounting:** after all claims and withdrawals, `get_accounting`
   reports balance, liabilities and unaccounted surplus all equal to 0.
8. **Attack check:** on another market, `resolve_market` with
   `https://attacker.example\@<allowlisted-domain>/x` reverts before any
   fetch.

---

## License

MIT, see `LICENSE`.
