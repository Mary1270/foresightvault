"""
End-to-end: create -> stake -> resolve (mocked web/LLM) -> claim ->
withdraw, plus consensus disagreement, evidence grounding, the voting-set
lock, refund paths and a full-lifecycle solvency invariant.
"""
import json
import unittest

from _bootstrap import (
    ALICE_ADDRESS, BOB_ADDRESS, CAROL_ADDRESS, STRANGER_ADDRESS,
    ConsensusFailure, call, call_payable, gl, make_contract, reset_transfers, set_caller,
)
from _helpers import (
    ANSWERS, AP_URL, BBC_URL, REUTERS_URL, close_resolution_window, create_market,
    market, mocked_sources, open_resolution_window, resolve,
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


class EvidenceQualityTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)

    def flags(self):
        return [r["quality_flag"] for r in market(self.c, self.mid)["records"]]

    def test_confirmed_grounded_sources_resolve(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        self.assertEqual(m["status"], "resolved")
        self.assertEqual(m["winning_outcome_index"], 0)
        self.assertEqual(m["final_classification"], "Candidate A wins")
        self.assertTrue(all(r["quote"] for r in m["records"]))

    def test_projections_and_polls_never_count(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "POLL", AP_URL: "POLL"})
        self.assertEqual(m["status"], "staking")
        self.assertEqual(m["final_classification"], "Indeterminate")
        self.assertEqual(self.flags(), ["not_confirmed", "not_confirmed"])

    def test_fabricated_quote_never_counts(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"},
                    answer_override={"A": ANSWERS["FABRICATED"]})
        self.assertEqual(m["status"], "staking")
        self.assertEqual(self.flags(), ["quote_not_found", "quote_not_found"])

    def test_irrelevant_sources_never_count(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "IRRELEVANT", AP_URL: "IRRELEVANT"})
        self.assertEqual(self.flags(), ["not_relevant", "not_relevant"])
        self.assertIsNone(m["locked_source_urls"])

    def test_single_good_source_is_not_enough(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "IRRELEVANT"})
        self.assertEqual(m["status"], "staking")

    def test_split_sources_are_indeterminate_but_lock(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        self.assertEqual(m["final_classification"], "Indeterminate")
        self.assertIsNotNone(m["locked_source_urls"])

    def test_fetch_errors_and_model_errors_are_contained(self):
        with mocked_sources({REUTERS_URL: TimeoutError("timed out"), AP_URL: "A"}):
            m = json.loads(call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL]))
        self.assertEqual(m["records"][0]["fetch_status"], "timeout")
        self.assertEqual(m["status"], "staking")

        from unittest.mock import patch
        with patch.object(gl.nondet.web, "render", side_effect=lambda url, mode="text": "Local news. " + "word " * 20), \
                patch.object(gl.nondet, "exec_prompt", side_effect=RuntimeError("LLM down")):
            m = json.loads(call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL]))
        self.assertEqual([r["quality_flag"] for r in m["records"]], ["model_error", "model_error"])

    def test_prompt_injection_text_in_source_does_not_help_without_grounding(self):
        # Even if a hostile page makes the model claim an outcome, the quote
        # must exist on the page and two independent domains must agree.
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "IRRELEVANT"})
        self.assertEqual(m["status"], "staking")


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)

    def test_validator_disagreement_changes_nothing(self):
        before = self.c.markets[self.mid]
        with self.assertRaises(ConsensusFailure):
            with mocked_sources(
                {REUTERS_URL: "A", AP_URL: "A"},
                leader_then_validator=[{REUTERS_URL: "A", AP_URL: "A"},
                                       {REUTERS_URL: "B", AP_URL: "B"}],
            ):
                call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL])
        self.assertEqual(self.c.markets[self.mid], before)

    def test_validator_rejects_leader_winner_it_cannot_reproduce(self):
        # Leader sees a decisive result; validator's fetch fails -> no winner.
        with self.assertRaises(ConsensusFailure):
            with mocked_sources(
                {REUTERS_URL: "A", AP_URL: "A"},
                leader_then_validator=[{REUTERS_URL: "A", AP_URL: "A"},
                                       {REUTERS_URL: "A", AP_URL: TimeoutError("timed out")}],
            ):
                call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL])
        self.assertEqual(market(self.c, self.mid)["status"], "staking")

    def test_honest_leader_and_validators_agree(self):
        # Same decision from independently fetched pages is accepted even if
        # audit details differ.
        m = resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A"})
        self.assertEqual(m["status"], "resolved")

    def test_validator_rejects_malformed_leader_output(self):
        leader_payload = json.dumps({"records": [], "winning_outcome_index": "0",
                                     "independent_source_count": 2, "lock_eligible": True})
        from _bootstrap import M

        # Simulate a dishonest leader: validator must refuse it.
        class FakeReturn(gl.vm.Return):
            pass

        with mocked_sources({REUTERS_URL: "A", AP_URL: "A"}):
            captured = {}

            def run(leader_fn, validator_fn, /):
                captured["ok"] = validator_fn(FakeReturn(leader_payload))
                raise ConsensusFailure("rejected")

            from unittest.mock import patch
            with patch.object(gl.vm, "run_nondet_unsafe", side_effect=run):
                with self.assertRaises(ConsensusFailure):
                    call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL])
        self.assertFalse(captured["ok"])

    def test_validator_rejects_non_return_object_even_with_valid_payload(self):
        # An error/rollback result must never be accepted, even if it happens
        # to carry a payload identical to an honest result.
        honest = json.dumps({"records": [], "winning_outcome_index": 0,
                             "independent_source_count": 2, "lock_eligible": True})

        class NotAReturn:
            calldata = honest

        with mocked_sources({REUTERS_URL: "A", AP_URL: "A"}):
            captured = {}

            def run(leader_fn, validator_fn, /):
                captured["ok"] = validator_fn(NotAReturn())
                raise ConsensusFailure("rejected")

            from unittest.mock import patch
            with patch.object(gl.vm, "run_nondet_unsafe", side_effect=run):
                with self.assertRaises(ConsensusFailure):
                    call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL])
        self.assertFalse(captured["ok"])

    def test_validator_rejects_non_return_result(self):
        with mocked_sources({REUTERS_URL: "A", AP_URL: "A"}):
            captured = {}

            def run(leader_fn, validator_fn, /):
                captured["ok"] = validator_fn(object())
                raise ConsensusFailure("rejected")

            from unittest.mock import patch
            with patch.object(gl.vm, "run_nondet_unsafe", side_effect=run):
                with self.assertRaises(ConsensusFailure):
                    call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL])
        self.assertFalse(captured["ok"])


class MaliciousLeaderTests(unittest.TestCase):
    """A dishonest leader tampers with its own result. Validators must reject
    every variant, and nothing may change."""

    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 1, GEN)
        open_resolution_window(self.c, self.mid)

    def attempt(self, tamper):
        from unittest.mock import patch
        verdict = {}

        def run(leader_fn, validator_fn, /):
            payload = json.loads(leader_fn())
            tamper(payload)
            verdict["ok"] = validator_fn(gl.vm.Return(json.dumps(payload)))
            if verdict["ok"] is not True:
                raise ConsensusFailure("rejected")
            return json.dumps(payload)

        before = self.c.markets[self.mid]
        with mocked_sources({REUTERS_URL: "B", AP_URL: "B"}):
            with patch.object(gl.vm, "run_nondet_unsafe", side_effect=run):
                with self.assertRaises(ConsensusFailure):
                    call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL])
        self.assertFalse(verdict["ok"])
        self.assertEqual(self.c.markets[self.mid], before)

    def test_bool_winner_that_equals_real_index_is_rejected(self):
        # True == 1 in Python; accepting it would store an unclaimable winner
        # and lock the pot forever.
        self.attempt(lambda p: p.__setitem__("winning_outcome_index", True))

    def test_wrong_winner_rejected(self):
        self.attempt(lambda p: p.__setitem__("winning_outcome_index", 0))

    def test_suppressed_winner_rejected(self):
        self.attempt(lambda p: p.__setitem__("winning_outcome_index", None))

    def test_out_of_range_winner_rejected(self):
        self.attempt(lambda p: p.__setitem__("winning_outcome_index", 99))

    def test_float_winner_rejected(self):
        self.attempt(lambda p: p.__setitem__("winning_outcome_index", 1.0))

    def test_flipped_lock_flag_rejected(self):
        self.attempt(lambda p: p.__setitem__("lock_eligible", False))

    def test_bool_or_bad_count_rejected(self):
        self.attempt(lambda p: p.__setitem__("independent_source_count", "2"))
        self.attempt(lambda p: p.__setitem__("independent_source_count", True))

    def test_bad_records_rejected(self):
        self.attempt(lambda p: p.__setitem__("records", "not a list"))
        self.attempt(lambda p: p.__setitem__("records", [1, 2]))

    def test_missing_fields_rejected(self):
        self.attempt(lambda p: p.pop("lock_eligible"))

    def test_honest_result_still_accepted_and_claimable(self):
        m = resolve(self.c, self.mid, {REUTERS_URL: "B", AP_URL: "B"})
        self.assertEqual(m["winning_outcome_index"], 1)
        self.assertIs(type(m["winning_outcome_index"]), int)
        self.assertEqual(claim(self.c, self.mid, ALICE_ADDRESS), GEN)


class ExactLockComparisonTests(unittest.TestCase):
    def test_locked_url_with_different_path_case_rejected(self):
        c = make_contract()
        mid = create_market(c)
        stake(c, mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(c, mid)
        resolve(c, mid, {REUTERS_URL: "A", AP_URL: "B"})  # locks
        swapped = REUTERS_URL.replace("/world/", "/World/")
        with self.assertRaises(gl.vm.UserError):
            resolve(c, mid, {swapped: "A", AP_URL: "A"})


class SourceLockTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        stake(self.c, self.mid, ALICE_ADDRESS, 0, GEN)
        open_resolution_window(self.c, self.mid)

    def test_irrelevant_first_attempt_does_not_lock(self):
        resolve(self.c, self.mid, {REUTERS_URL: "IRRELEVANT", AP_URL: "IRRELEVANT"})
        m = resolve(self.c, self.mid, {"https://reuters.com/other": "A", "https://apnews.com/other": "A"})
        self.assertEqual(m["status"], "resolved")

    def test_locked_set_must_be_resubmitted_exactly(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})  # locks, indeterminate
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A", "https://apnews.com/other": "A"})
        with self.assertRaises(gl.vm.UserError):
            resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "A", BBC_URL: "A"})

    def test_locked_set_accepts_reordered_resubmission(self):
        resolve(self.c, self.mid, {REUTERS_URL: "A", AP_URL: "B"})
        m = resolve(self.c, self.mid, {AP_URL: "A", REUTERS_URL: "A"})
        self.assertEqual(m["status"], "resolved")
        self.assertEqual(m["resolution_attempts"], 2)


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
