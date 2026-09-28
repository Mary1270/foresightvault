"""
stake(): value floor, deadline/status/index checks, and position/pool
accounting. Positions live outside the market record, so any number of
stakers can join without growing the record or blocking anyone.
"""
import json
import unittest

from _bootstrap import (
    ALICE_ADDRESS, BOB_ADDRESS, CAROL_ADDRESS, Address, call_payable, gl, make_contract, set_caller,
)
from _helpers import close_staking_only, create_market, market


class StakeValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        set_caller(ALICE_ADDRESS)

    def test_below_minimum_rejected(self):
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", int(self.c.MIN_STAKE_WEI) - 1, self.mid, 0)

    def test_zero_value_rejected(self):
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", 0, self.mid, 0)

    def test_exact_minimum_accepted(self):
        call_payable(self.c, "stake", int(self.c.MIN_STAKE_WEI), self.mid, 0)
        self.assertEqual(market(self.c, self.mid)["outcome_pools"][0], str(int(self.c.MIN_STAKE_WEI)))

    def test_unknown_market_rejected(self):
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", 10**16, "999", 0)

    def test_bool_and_non_int_indexes_rejected(self):
        # True would otherwise be stored under key "True" while pools use
        # index 1, making the stake unclaimable and unrefundable.
        for index in (True, False, 1.0, "1", None):
            with self.assertRaises(gl.vm.UserError):
                call_payable(self.c, "stake", 10**16, self.mid, index)
        self.assertEqual(len(self.c.positions), 0)

    def test_index_out_of_range_rejected(self):
        for index in (-1, 3):
            with self.assertRaises(gl.vm.UserError):
                call_payable(self.c, "stake", 10**16, self.mid, index)

    def test_after_staking_deadline_rejected(self):
        close_staking_only(self.c, self.mid)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", 10**16, self.mid, 0)

    def test_on_settled_market_rejected(self):
        record = json.loads(self.c.markets[self.mid])
        record["status"] = "resolved"
        self.c.markets[self.mid] = json.dumps(record)
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", 10**16, self.mid, 0)

    def test_rejected_stake_does_not_change_balance_or_pools(self):
        before = self.c.balance
        with self.assertRaises(gl.vm.UserError):
            call_payable(self.c, "stake", 1, self.mid, 0)
        self.assertEqual(self.c.balance, before)
        self.assertEqual(market(self.c, self.mid)["total_pool"], "0")


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
