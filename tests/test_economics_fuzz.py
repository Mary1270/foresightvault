"""
Seeded randomized tests of the money logic: across many random markets
(random stakers, amounts, hedging, winning outcome, claim order and
resolved-vs-refunded ending), every wei taken in is paid back out, nobody
can claim twice, and no eligible participant is ever left unable to claim.
"""
import json
import random
import unittest

from _bootstrap import call, call_payable, gl, make_contract, reset_transfers, set_caller
from _helpers import (
    AP_URL, REUTERS_URL, close_resolution_window, create_market, open_resolution_window, resolve,
)

OUTCOME_PAGES = {0: "A", 1: "B"}


class EconomicsFuzzTests(unittest.TestCase):
    def run_one(self, seed):
        rng = random.Random(seed)
        reset_transfers()
        c = make_contract()
        mid = create_market(c)
        wallets = ["0x" + format(rng.randrange(1, 2**160), "040x") for _ in range(rng.randint(1, 12))]
        taken_in = 0
        for who in wallets:
            for _ in range(rng.randint(1, 3)):
                value = rng.choice([10**15, 10**15 + rng.randint(0, 10**6), rng.randint(10**15, 5 * 10**18)])
                set_caller(who)
                call_payable(c, "stake", value, mid, rng.randrange(3))
                taken_in += value

        ending = rng.choice(["resolve_A", "resolve_B", "expire"])
        if ending == "expire":
            close_resolution_window(c, mid)
            call(c, "expire_market", mid)
        else:
            open_resolution_window(c, mid)
            page = "A" if ending == "resolve_A" else "B"
            resolve(c, mid, {REUTERS_URL: page, AP_URL: page})

        order = wallets[:]
        rng.shuffle(order)
        for who in order:
            set_caller(who)
            if int(c.get_claimable(mid, who)) > 0:
                call(c, "claim", mid)
                with self.assertRaises(gl.vm.UserError):
                    call(c, "claim", mid)
            self.assertEqual(c.get_claimable(mid, who), "0")
            if int(c.get_pending_withdrawal(who)) > 0:
                call(c, "withdraw")

        paid_out = sum(int(t["value"]) for t in gl.evm.transfers)
        self.assertEqual(paid_out, taken_in, f"seed {seed} ending {ending}")
        for who in wallets:
            self.assertEqual(c.get_pending_withdrawal(who), "0")

    def test_many_random_markets(self):
        for seed in range(300):
            with self.subTest(seed=seed):
                self.run_one(seed)


if __name__ == "__main__":
    unittest.main()
