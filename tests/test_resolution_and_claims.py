"""
End-to-end: create -> stake -> resolve (mocked web/LLM) -> claim ->
withdraw, plus validator-backed evidence, evidence anchoring, refund paths
and a full-lifecycle solvency invariant.
"""
import json
import unittest

from _bootstrap import (
    M, ALICE_ADDRESS, BOB_ADDRESS, CAROL_ADDRESS, STRANGER_ADDRESS,
    ConsensusFailure, call, call_payable, gl, make_contract, reset_transfers, set_caller,
)
from _helpers import (
    ANSWERS, AP_URL, BBC_URL, GUARDIAN_URL, NPR_URL, REUTERS_URL, close_resolution_window,
    create_market, market, mocked_sources, open_resolution_window, resolve,
)

GEN = 10**18


def stake(c, mid, who, index, value):
    set_caller(who)
    call_payable(c, "stake", value, mid, index)


def claim(c, mid, who):
    set_caller(who)
    return int(call(c, "claim", mid))


class ResolutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)

    def test_cannot_resolve_before_window(self):
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})

    def test_cannot_resolve_after_window(self):
        close_resolution_window(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})

    def test_source_count_bounds(self):
        open_resolution_window(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A"})

    def test_duplicate_domain_rejected(self):
        open_resolution_window(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "resolve_market", self.mid,
                 [REUTERS_URL, "https://reuters.com/other", AP_URL])

    def test_committed_domain_missing_rejected(self):
        open_resolution_window(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A", BBC_URL: "A"})

    def test_cannot_resolve_twice(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})


def run_with_leader(c, mid, pages, tamper=None, **mock_kwargs):
    """Run resolve_market with a real validator pass, optionally tampering
    with the leader's proposal first. Returns (validator_verdict, raised)."""
    from unittest.mock import patch
    verdict = {}

    def run(leader_fn, validator_fn, /):
        proposal = json.loads(leader_fn())
        if tamper:
            tamper(proposal)
        verdict["ok"] = validator_fn(gl.vm.Return(json.dumps(proposal)))
        if verdict["ok"] is not True:
            raise ConsensusFailure("validators rejected the leader")
        return json.dumps(proposal)

    with mocked_sources(pages, **mock_kwargs):
        with patch.object(gl.vm, "run_nondet_unsafe", side_effect=run):
            try:
                call(c, "resolve_market", mid, list(pages.keys()))
                return verdict.get("ok"), None
            except Exception as exc:
                return verdict.get("ok"), exc


class EvidenceQualityTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)

    def votes(self, m):
        return [r["vote"] for r in m["last_attempt"]]

    def test_confirmed_grounded_sources_resolve(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        self.assertEqual(m["status"], "resolved")
        self.assertEqual(m["winning_outcome_index"], 0)
        self.assertEqual(m["final_classification"], "Candidate A wins")
        self.assertEqual(m["independent_source_count"], 2)
        for url in (REUTERS_URL, AP_URL):
            self.assertEqual(m["evidence"][url]["vote"], 0)
            self.assertIn("certified that Candidate A won", m["evidence"][url]["quote"])

    def test_projections_and_polls_never_count(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "POLL", AP_URL: "POLL"})
        self.assertEqual((m["status"], m["final_classification"]), ("staking", "Indeterminate"))
        self.assertEqual(self.votes(m), [None, None])
        self.assertEqual(m["evidence"], {})

    def test_fabricated_quote_never_counts(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"},
                    answer_override={"A": ANSWERS["FABRICATED"]})
        self.assertEqual(self.votes(m), [None, None])
        self.assertEqual(m["status"], "staking")

    def test_irrelevant_sources_never_count(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "IRRELEVANT", AP_URL: "IRRELEVANT"})
        self.assertEqual(self.votes(m), [None, None])
        self.assertEqual(m["recorded_source_urls"], [])

    def test_single_good_source_is_not_enough(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "IRRELEVANT"})
        self.assertEqual(m["status"], "staking")
        self.assertEqual(m["independent_source_count"], 1)

    def test_fetch_errors_and_model_errors_are_contained(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: TimeoutError("timed out"), AP_URL: "A"})
        self.assertEqual(self.votes(m), [None, 0])
        self.assertEqual(m["status"], "staking")
        from unittest.mock import patch
        with patch.object(gl.nondet.web, "render", side_effect=lambda url, mode="text": "Local news. " + "word " * 20), \
                patch.object(gl.nondet, "exec_prompt", side_effect=RuntimeError("LLM down")):
            # AP already has recorded evidence, so submit Reuters (no vote yet) + BBC.
            m = json.loads(call(self.c, "resolve_market", self.mid, [REUTERS_URL, BBC_URL]))
        self.assertEqual(self.votes(m), [None, None])
        self.assertEqual(m["recorded_source_urls"], [AP_URL])


class ValidatorBackedEvidenceTests(unittest.TestCase):
    """Steward request 2: everything stored as evidence must be something every
    validator independently reproduced or verified, and the count and winner
    must be recomputed by the contract, not taken from the leader."""

    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)
        self.pages = {REUTERS_URL: "A", AP_URL: "A"}

    def assertRejected(self, tamper=None, **kw):
        before = self.c.markets[self.mid]
        ok, exc = run_with_leader(self.c, self.mid, self.pages, tamper, **kw)
        self.assertFalse(ok)
        self.assertIsInstance(exc, ConsensusFailure)
        self.assertEqual(self.c.markets[self.mid], before, "a rejected proposal must change nothing")

    def test_honest_leader_accepted(self):
        ok, exc = run_with_leader(self.c, self.mid, self.pages)
        self.assertTrue(ok)
        self.assertIsNone(exc)

    def test_leader_cannot_alter_a_quote_even_keeping_the_same_winner(self):
        def tamper(p):
            p["votes"][0]["quote"] = "Officials announced a landslide victory for Candidate A today."
        self.assertRejected(tamper)

    def test_leader_cannot_substitute_another_real_sentence_that_does_not_report_it(self):
        # The sentence IS on the page, but the validator's own model says it
        # does not report the outcome -> rejected.
        def tamper(p):
            p["votes"][0]["quote"] = "The city council met later in the week to discuss the budget"
        self.assertRejected(tamper, excerpt_answer="No")

    def test_leader_cannot_suppress_a_counted_source(self):
        def tamper(p):
            p["votes"][1] = {"url": AP_URL, "vote": None, "quote": ""}
        self.assertRejected(tamper)

    def test_leader_cannot_invent_a_vote(self):
        self.pages = {REUTERS_URL: "A", AP_URL: "IRRELEVANT"}

        def tamper(p):
            p["votes"][1] = {"url": AP_URL, "vote": 0, "quote": ANSWERS["A"].split("QUOTE: ")[1]}
        self.assertRejected(tamper)

    def test_leader_cannot_flip_a_vote(self):
        def tamper(p):
            p["votes"][0]["vote"] = 1
        self.assertRejected(tamper)

    def test_leader_cannot_add_unverified_fields(self):
        def tamper(p):
            p["independent_source_count"] = 5
        self.assertRejected(tamper)

        def tamper_record(p):
            p["votes"][0]["fetch_status"] = "ok"
        self.assertRejected(tamper_record)

    def test_type_confusion_rejected(self):
        for bad in (True, 0.0, "0"):
            def tamper(p, bad=bad):
                p["votes"][0]["vote"] = bad
            self.assertRejected(tamper)

    def test_quote_must_be_on_the_validators_own_fetch(self):
        # Leader fetched page A; the validator's fetch of the first source is
        # different content, so the leader's quote is not on it.
        self.assertRejected(leader_then_validator=[{REUTERS_URL: "A", AP_URL: "A"},
                                                   {REUTERS_URL: "A2", AP_URL: "A"}])

    def test_validator_disagreeing_on_any_source_rejects(self):
        self.assertRejected(leader_then_validator=[{REUTERS_URL: "A", AP_URL: "A"},
                                                   {REUTERS_URL: "A", AP_URL: TimeoutError("timed out")}])

    def test_validator_rejects_non_return_result(self):
        captured = {}
        from unittest.mock import patch

        def run(leader_fn, validator_fn, /):
            honest = leader_fn()

            class NotAReturn:
                calldata = honest
            captured["ok"] = validator_fn(NotAReturn())
            raise ConsensusFailure("rejected")

        with mocked_sources(self.pages):
            with patch.object(gl.vm, "run_nondet_unsafe", side_effect=run):
                with self.assertRaises(ConsensusFailure):
                    call(self.c, "resolve_market", self.mid, list(self.pages))
        self.assertFalse(captured["ok"])

    def test_stored_evidence_contains_only_verified_fields(self):
        m = resolve(self.c, self.mid, self.pages)
        for record in m["evidence"].values():
            self.assertEqual(set(record), {"url", "domain", "path", "vote", "quote", "attempt"})
        for record in m["last_attempt"]:
            self.assertEqual(set(record), {"url", "domain", "vote", "quote"})
        self.assertNotIn("records", m)

    def test_count_and_winner_are_recomputed_by_the_contract(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A", BBC_URL: "B"})
        self.assertEqual(m["independent_source_count"], 3)
        self.assertEqual(m["winning_outcome_index"], 0)


class ImmutableEvidenceTests(unittest.TestCase):
    """Steward request 1: conflicting first evidence must not freeze a market,
    and agreed evidence (including dissent) must never be removable or
    rewritable. Recorded votes are immutable; later attempts add only new
    sources from new outlets; the ledger is capped at 6 sources."""

    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        stake(self.c, self.mid, BOB_ADDRESS, 1, GEN)
        open_resolution_window(self.c, self.mid)

    def test_steward_scenario_conflicting_first_set_can_be_recovered(self):
        # A permissionless resolver submits two valid sources that disagree.
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        self.assertEqual((m["status"], m["final_classification"]), ("staking", "Indeterminate"))
        self.assertEqual(m["recorded_source_urls"], sorted([REUTERS_URL, AP_URL]))
        # Later decisive evidence can still be added: the market resolves.
        m = resolve(self.c, self.mid, {BBC_URL: "A"})
        self.assertEqual(m["status"], "resolved")
        self.assertEqual(m["winning_outcome_index"], 0)
        self.assertEqual(m["independent_source_count"], 3)

    def test_no_later_attempt_can_overwrite_a_recorded_vote(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        before = market(self.c, self.mid)["evidence"]
        # Even if the page now reports the other outcome, it cannot be
        # resubmitted, so its recorded vote can never change.
        for pages in ({AP_URL: "A"}, {AP_URL: "A", BBC_URL: "A"}, {REUTERS_URL: "B", BBC_URL: "B"}):
            with self.assertRaises(gl.vm.UserError):
                resolve(self.c, self.mid, pages)
        self.assertEqual(market(self.c, self.mid)["evidence"], before)

    def test_contract_never_overwrites_even_if_a_recorded_url_reaches_it(self):
        # Defense in depth: bypass the pre-fetch check and feed an agreed vote
        # for an already-recorded URL straight into the ledger update.
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        from unittest.mock import patch
        original = self.c._validate_source_urls

        def no_check(mkt, urls):
            return [(u, M.parse_source_url(u)[0], M.parse_source_url(u)[1]) for u in urls]

        with patch.object(self.c, "_validate_source_urls", side_effect=no_check):
            m = resolve(self.c, self.mid, {AP_URL: "A", BBC_URL: "A"})
        self.assertEqual(m["evidence"][AP_URL]["vote"], 1)
        self.assertEqual(m["evidence"][AP_URL]["attempt"], 1)
        del original

    def test_dissent_cannot_be_diluted_by_another_page_from_the_same_outlet(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {"https://apnews.com/other": "A"})
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {"https://www.reuters.com/other": "A"})

    def test_recorded_sources_are_not_refetched(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        with mocked_sources({BBC_URL: "A"}) as state:
            call(self.c, "resolve_market", self.mid, [BBC_URL])
        # One fetch by the leader and one by the validator, for BBC only.
        self.assertEqual(state["urls_seen"], 2)

    def test_first_attempt_needs_two_sources_later_attempts_one(self):
        with self.assertRaisesRegex(gl.vm.UserError, "between 2 and 6"):
            resolve(self.c, self.mid, {REUTERS_URL: "A"})
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        m = resolve(self.c, self.mid, {BBC_URL: "A"})
        self.assertEqual(m["status"], "resolved")

    def test_committed_domains_are_covered_by_recorded_or_new_sources(self):
        # Committed: reuters + apnews. Reuters counts, AP does not.
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "IRRELEVANT"})
        self.assertEqual(m["recorded_source_urls"], [REUTERS_URL])
        with self.assertRaises(gl.vm.UserError):      # AP still uncovered
            resolve(self.c, self.mid, {BBC_URL: "A"})
        m = resolve(self.c, self.mid, {"https://apnews.com/other": "A"})
        self.assertEqual(m["status"], "resolved")

    def test_sources_without_an_agreed_vote_are_not_recorded_and_may_be_retried(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "IRRELEVANT", AP_URL: "POLL"})
        self.assertEqual(m["recorded_source_urls"], [])
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        self.assertEqual(m["status"], "resolved")

    def test_dissent_still_blocks_a_tie(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        m = resolve(self.c, self.mid, {BBC_URL: "IRRELEVANT"})
        self.assertEqual(m["status"], "staking")
        self.assertEqual(m["independent_source_count"], 2)

    def test_ledger_is_capped_at_six_sources(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        resolve(self.c, self.mid, {BBC_URL: "A", GUARDIAN_URL: "B"})
        with self.assertRaises(gl.vm.UserError):     # 3 more would exceed 6
            resolve(self.c, self.mid, {NPR_URL: "A", "https://www.axios.com/x": "B", "https://www.cnbc.com/x": "A"})
        m = resolve(self.c, self.mid, {NPR_URL: "A", "https://www.axios.com/x": "B"})   # 3 vs 3
        self.assertEqual((len(m["recorded_source_urls"]), m["status"]), (6, "staking"))
        with self.assertRaisesRegex(gl.vm.UserError, "already has 6 recorded sources"):
            resolve(self.c, self.mid, {"https://www.cnbc.com/x": "A"})
        close_resolution_window(self.c, self.mid)
        self.assertEqual(json.loads(call(self.c, "expire_market", self.mid))["status"], "refunding")

    def test_decisive_result_is_final(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        self.assertEqual(m["status"], "resolved")
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {BBC_URL: "B", GUARDIAN_URL: "B"})
        self.assertEqual(market(self.c, self.mid)["winning_outcome_index"], 0)

    def test_attempt_number_recorded_per_evidence(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        m = resolve(self.c, self.mid, {BBC_URL: "A"})
        self.assertEqual(m["evidence"][BBC_URL]["attempt"], 2)
        self.assertEqual(m["evidence"][AP_URL]["attempt"], 1)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        reset_transfers()
        self.c = make_contract()
        self.mid = create_market(self.c)

    def test_proportional_payout_losers_get_nothing(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, 30 * 10**15)
        stake(self.c, self.mid, BOB_ADDRESS, 0, 10 * 10**15)
        stake(self.c, self.mid, CAROL_ADDRESS, 1, 60 * 10**15)
        open_resolution_window(self.c, self.mid)
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})

        self.assertEqual(self.c.get_claimable(self.mid, ALICE_ADDRESS), str(75 * 10**15))
        self.assertEqual(claim(self.c, self.mid, ALICE_ADDRESS), 75 * 10**15)
        self.assertEqual(claim(self.c, self.mid, BOB_ADDRESS), 25 * 10**15)
        with self.assertRaises(gl.vm.UserError):
            claim(self.c, self.mid, CAROL_ADDRESS)
        self.assertEqual(self.c.get_claimable(self.mid, CAROL_ADDRESS), "0")

    def test_double_claim_rejected(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        claim(self.c, self.mid, ALICE_ADDRESS)
        with self.assertRaises(gl.vm.UserError):
            claim(self.c, self.mid, ALICE_ADDRESS)
        self.assertTrue(self.c.has_claimed(self.mid, ALICE_ADDRESS))
        self.assertEqual(self.c.get_claimable(self.mid, ALICE_ADDRESS), "0")

    def test_cannot_claim_unsettled_market(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        with self.assertRaises(gl.vm.UserError):
            claim(self.c, self.mid, ALICE_ADDRESS)

    def test_rounding_remainder_goes_to_last_winning_claimer(self):
        for who in (ALICE_ADDRESS, BOB_ADDRESS, CAROL_ADDRESS):
            stake(self.c, self.mid, who, 0, 10**15)
        stake(self.c, self.mid, STRANGER_ADDRESS, 1, 10**15 + 1)
        open_resolution_window(self.c, self.mid)
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        total = int(market(self.c, self.mid)["total_pool"])
        paid = [claim(self.c, self.mid, who) for who in (BOB_ADDRESS, CAROL_ADDRESS, ALICE_ADDRESS)]
        self.assertEqual(sum(paid), total)
        self.assertEqual(paid[0], paid[1])
        self.assertGreater(paid[2], paid[0])
        self.assertEqual(market(self.c, self.mid)["winning_paid_total"], str(total))

    def test_hedger_claims_only_winning_side(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, 10**16)
        stake(self.c, self.mid, ALICE_ADDRESS, 1, 30 * 10**15)
        stake(self.c, self.mid, BOB_ADDRESS, 0, 10**16)
        open_resolution_window(self.c, self.mid)
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        # total 50e15, winning pool 20e15, Alice holds half of it.
        self.assertEqual(claim(self.c, self.mid, ALICE_ADDRESS), 25 * 10**15)


class RefundTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)

    def test_no_stakers_on_real_outcome_refunds_everyone(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 1, 30 * 10**15)
        stake(self.c, self.mid, BOB_ADDRESS, 2, 20 * 10**15)
        open_resolution_window(self.c, self.mid)
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        self.assertEqual((m["status"], m["refund_reason"]), ("refunding", "no_winning_stakers"))
        self.assertEqual(claim(self.c, self.mid, ALICE_ADDRESS), 30 * 10**15)
        self.assertEqual(claim(self.c, self.mid, BOB_ADDRESS), 20 * 10**15)

    def test_expire_only_after_window_then_full_refunds(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, 15 * 10**15)
        stake(self.c, self.mid, ALICE_ADDRESS, 2, 5 * 10**15)
        stake(self.c, self.mid, BOB_ADDRESS, 1, 25 * 10**15)
        open_resolution_window(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "expire_market", self.mid)
        close_resolution_window(self.c, self.mid)
        set_caller(STRANGER_ADDRESS)  # permissionless
        m = json.loads(call(self.c, "expire_market", self.mid))
        self.assertEqual((m["status"], m["refund_reason"]), ("refunding", "expired"))
        self.assertEqual(claim(self.c, self.mid, ALICE_ADDRESS), 20 * 10**15)
        self.assertEqual(claim(self.c, self.mid, BOB_ADDRESS), 25 * 10**15)
        with self.assertRaises(gl.vm.UserError):
            claim(self.c, self.mid, STRANGER_ADDRESS)
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "expire_market", self.mid)

    def test_resolved_market_cannot_be_expired(self):
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        close_resolution_window(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "expire_market", self.mid)


class WithdrawTests(unittest.TestCase):
    def setUp(self):
        reset_transfers()
        self.c = make_contract()

    def test_withdraw_transfers_zeroes_and_rejects_second_call(self):
        mid = create_market(self.c)
        stake(self.c, mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, mid)
        resolve(self.c, mid, {REUTERS_URL: "A", AP_URL: "A"})
        claim(self.c, mid, ALICE_ADDRESS)
        self.assertEqual(self.c.get_pending_withdrawal(ALICE_ADDRESS), str(GEN))
        set_caller(ALICE_ADDRESS)
        call(self.c, "withdraw")
        self.assertEqual(len(gl.evm.transfers), 1)
        self.assertEqual(int(gl.evm.transfers[0]["value"]), GEN)
        self.assertEqual(gl.evm.transfers[0]["to"].lower(), ALICE_ADDRESS.lower())
        self.assertEqual(self.c.get_pending_withdrawal(ALICE_ADDRESS), "0")
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "withdraw")

    def test_nothing_to_withdraw(self):
        set_caller(STRANGER_ADDRESS)
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "withdraw")


class SolvencyInvariantTests(unittest.TestCase):
    """Across two markets (one resolved, one refunded) with many stakers, the
    GEN paid out equals the GEN taken in, to the wei."""

    def test_everything_in_equals_everything_out(self):
        reset_transfers()
        c = make_contract()
        m1, m2 = create_market(c), create_market(c)
        stakers = ["0x" + format(i + 7, "040x") for i in range(25)]
        taken_in = 0
        for i, who in enumerate(stakers):
            for mid in (m1, m2):
                value = 10**15 + i * 7919
                stake(c, mid, who, i % 3, value)
                taken_in += value
        self.assertEqual(int(c.get_contract_balance()), taken_in)

        open_resolution_window(c, m1)
        resolve(c, m1, {REUTERS_URL: "A", AP_URL: "A"})
        close_resolution_window(c, m2)
        call(c, "expire_market", m2)

        for who in stakers:
            for mid in (m1, m2):
                set_caller(who)
                if int(c.get_claimable(mid, who)) > 0:
                    call(c, "claim", mid)
            if int(c.get_pending_withdrawal(who)) > 0:
                set_caller(who)
                call(c, "withdraw")

        paid_out = sum(int(t["value"]) for t in gl.evm.transfers)
        self.assertEqual(paid_out, taken_in)


if __name__ == "__main__":
    unittest.main()
