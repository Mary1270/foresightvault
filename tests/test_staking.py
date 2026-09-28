"""
stake(): value floor, deadline/status/index checks, and position/pool
accounting. Positions live outside the market record, so any number of
stakers can join without growing the record or blocking anyone.
"""
import json
import unittest

from _bootstrap import (
    ALICE_ADDRESS, BOB_ADDRESS, CAROL_ADDRESS, call_payable, gl, make_contract, reset_transfers,
    set_caller, settle_transfers, u256,
)
from _helpers import close_staking_only, create_market, market


class StakeValidationTests(unittest.TestCase):
    """Invalid stakes with GEN attached must not revert: on GenLayer a
    reverted payable call keeps the GEN in the contract with no record of
    the sender (observed live). They are refunded via pending_withdrawals."""

    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        set_caller(ALICE_ADDRESS)

    def assertRefunded(self, value, market_id, index, reason_fragment):
        before_pending = int(self.c.get_pending_withdrawal(ALICE_ADDRESS))
        result = json.loads(call_payable(self.c, "stake", value, market_id, index))
        self.assertFalse(result["accepted"])
        self.assertIn(reason_fragment, result["reason"])
        self.assertEqual(result["refunded_wei"], str(value))
        self.assertEqual(int(self.c.get_pending_withdrawal(ALICE_ADDRESS)), before_pending + value)
        self.assertEqual(len(self.c.positions), 0)
        self.assertEqual(market(self.c, self.mid)["total_pool"], "0")

    def test_zero_value_reverts_because_nothing_to_protect(self):
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", 0, self.mid, 0)

    def test_below_minimum_refunded(self):
        self.assertRefunded(int(self.c.MIN_STAKE_WEI) - 1, self.mid, 0, "Stake at least")

    def test_exact_minimum_accepted(self):
        result = json.loads(call_payable(self.c, "stake", int(self.c.MIN_STAKE_WEI), self.mid, 0))
        self.assertTrue(result["accepted"])
        self.assertEqual(market(self.c, self.mid)["outcome_pools"][0], str(int(self.c.MIN_STAKE_WEI)))

    def test_unknown_market_refunded(self):
        self.assertRefunded(10**16, "999", 0, "No market found")

    def test_index_out_of_range_refunded(self):
        for index in (-1, 3):
            self.assertRefunded(10**16, self.mid, index, "outcome_index must be")

    def test_bool_and_non_int_indexes_refunded(self):
        # True would otherwise be stored under key "True" while pools use
        # index 1, making the stake unclaimable and unrefundable.
        for index in (True, False, 1.0, "1", None):
            self.assertRefunded(10**16, self.mid, index, "outcome_index must be")

    def test_after_staking_deadline_refunded(self):
        close_staking_only(self.c, self.mid)
        self.assertRefunded(10**16, self.mid, 0, "Staking for this market has closed")

    def test_on_settled_market_refunded(self):
        record = json.loads(self.c.markets[self.mid])
        record["status"] = "resolved"
        self.c.markets[self.mid] = json.dumps(record)
        self.assertRefunded(10**16, self.mid, 0, "no longer accepting stakes")

    def test_refunded_stake_is_withdrawable_in_full(self):
        # Reproduces the live finding: 1 GEN attached to a stake that is
        # rejected (closed staking) must come back to the sender.
        close_staking_only(self.c, self.mid)
        call_payable(self.c, "stake", 10**18, self.mid, 0)
        reset_transfers()
        self.c.withdraw()
        self.assertEqual(int(gl.evm.transfers[0]["value"]), 10**18)
        self.assertEqual(gl.evm.transfers[0]["to"].lower(), ALICE_ADDRESS.lower())


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        reset_transfers()

    def accounting(self):
        return {k: int(v) for k, v in json.loads(self.c.get_accounting()).items()}

    def test_accepted_and_refunded_stakes_are_both_liabilities(self):
        set_caller(ALICE_ADDRESS)
        call_payable(self.c, "stake", 3 * 10**16, self.mid, 0)   # accepted
        call_payable(self.c, "stake", 2 * 10**16, self.mid, 9)   # refunded
        a = self.accounting()
        self.assertEqual(a["contract_balance"], 5 * 10**16)
        self.assertEqual(a["liabilities"], 5 * 10**16)
        self.assertEqual(a["unaccounted_surplus"], 0)

    def test_withdraw_reduces_liabilities_and_balance_together(self):
        set_caller(ALICE_ADDRESS)
        call_payable(self.c, "stake", 2 * 10**16, self.mid, 9)   # refunded
        self.c.withdraw()
        settle_transfers(self.c)
        a = self.accounting()
        self.assertEqual((a["contract_balance"], a["liabilities"], a["unaccounted_surplus"]), (0, 0, 0))
        self.assertEqual(a["total_withdrawn"], 2 * 10**16)

    def test_gen_from_a_reverted_call_shows_as_surplus_not_liability(self):
        # A zero-value stake reverts; simulate GEN arriving with a call that
        # reverted before contract logic ran: it is reported, never owed.
        set_caller(ALICE_ADDRESS)
        self.c.balance = self.c.balance + u256(10**18)
        a = self.accounting()
        self.assertEqual(a["liabilities"], 0)
        self.assertEqual(a["unaccounted_surplus"], 10**18)


class StakeAccountingTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)

    def stake(self, who, index, value):
        set_caller(who)
        call_payable(self.c, "stake", value, self.mid, index)

    def test_position_pool_and_total(self):
        self.stake(ALICE_ADDRESS, 1, 5 * 10**16)
        m = market(self.c, self.mid)
        self.assertEqual(m["outcome_pools"], ["0", str(5 * 10**16), "0"])
        self.assertEqual(m["total_pool"], str(5 * 10**16))
        self.assertEqual(m["outcome_staker_counts"], [0, 1, 0])
        self.assertEqual(json.loads(self.c.get_stake(self.mid, ALICE_ADDRESS)), {"1": str(5 * 10**16)})

    def test_repeat_stakes_add_up_and_count_once(self):
        self.stake(ALICE_ADDRESS, 0, 3 * 10**16)
        self.stake(ALICE_ADDRESS, 0, 2 * 10**16)
        self.assertEqual(json.loads(self.c.get_stake(self.mid, ALICE_ADDRESS))["0"], str(5 * 10**16))
        self.assertEqual(market(self.c, self.mid)["outcome_staker_counts"][0], 1)

    def test_same_wallet_different_case_is_one_position(self):
        self.stake("0x" + "ab" * 20, 0, 10**16)
        self.stake("0x" + "AB" * 20, 0, 10**16)
        self.assertEqual(market(self.c, self.mid)["outcome_staker_counts"][0], 1)
        self.assertEqual(json.loads(self.c.get_stake(self.mid, "0x" + "aB" * 20))["0"], str(2 * 10**16))

    def test_hedging_across_outcomes(self):
        self.stake(ALICE_ADDRESS, 0, 3 * 10**16)
        self.stake(ALICE_ADDRESS, 2, 4 * 10**16)
        self.assertEqual(json.loads(self.c.get_stake(self.mid, ALICE_ADDRESS)),
                         {"0": str(3 * 10**16), "2": str(4 * 10**16)})

    def test_totals_across_stakers(self):
        self.stake(ALICE_ADDRESS, 0, 3 * 10**16)
        self.stake(BOB_ADDRESS, 1, 5 * 10**16)
        self.stake(CAROL_ADDRESS, 2, 2 * 10**16)
        self.assertEqual(market(self.c, self.mid)["total_pool"], str(10 * 10**16))
        self.assertEqual(int(self.c.get_contract_balance()), 10 * 10**16)

    def test_many_stakers_never_blocked_and_record_stays_small(self):
        # No cap on participants; the market record does not grow with them.
        size_before = len(self.c.markets[self.mid])
        for i in range(500):
            self.stake("0x" + format(i + 1, "040x"), i % 3, 10**15)
        m = market(self.c, self.mid)
        self.assertEqual(sum(m["outcome_staker_counts"]), 500)
        self.assertEqual(int(m["total_pool"]), 500 * 10**15)
        self.assertLess(len(self.c.markets[self.mid]) - size_before, 100)


if __name__ == "__main__":
    unittest.main()
