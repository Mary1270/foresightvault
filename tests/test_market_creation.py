"""
Tests for create_market's validation logic: outcomes, question,
required_source_domains, and staking/resolution timing windows.
"""
import datetime
import unittest

from _bootstrap import CREATOR_ADDRESS, gl, make_contract, set_caller


def iso_in(seconds_from_now: float) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=seconds_from_now
    )
    return dt.isoformat()


def valid_kwargs(c, **overrides):
    kwargs = dict(
        question="Who will win the mayoral election?",
        outcomes=["Candidate A wins", "Candidate B wins", "Runoff required"],
        required_source_domains=["reuters.com", "apnews.com"],
        staking_deadline=iso_in(c.MIN_STAKING_LEAD_SECONDS + 60),
    )
    kwargs["resolution_deadline"] = iso_in(
        c.MIN_STAKING_LEAD_SECONDS + 60 + c.MIN_STAKING_TO_RESOLUTION_GAP_SECONDS + 60
    )
    kwargs.update(overrides)
    return kwargs


class CreateMarketHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        set_caller(CREATOR_ADDRESS)

    def test_creates_market_with_expected_fields(self):
        mid = self.c.create_market(**valid_kwargs(self.c))
        self.assertEqual(mid, "0")
        record = self.c.get_market(mid)
        self.assertIn("Candidate A wins", record)
        self.assertEqual(self.c.total_markets(), 1)

    def test_market_ids_increment(self):
        self.c.create_market(**valid_kwargs(self.c))
        mid2 = self.c.create_market(**valid_kwargs(self.c))
        self.assertEqual(mid2, "1")
        self.assertEqual(self.c.total_markets(), 2)

    def test_initial_status_is_staking(self):
        mid = self.c.create_market(**valid_kwargs(self.c))
        import json
        record = json.loads(self.c.get_market(mid))
        self.assertEqual(record["status"], "staking")
        self.assertEqual(record["outcome_pools"], ["0", "0", "0"])
        self.assertEqual(record["total_pool"], "0")


class OutcomeValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        set_caller(CREATOR_ADDRESS)

    def test_rejects_too_few_outcomes(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=["Only one"]))

    def test_rejects_too_many_outcomes(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(self.c, outcomes=[f"Option {i}" for i in range(7)])
            )

    def test_rejects_duplicate_outcomes_case_insensitive(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(self.c, outcomes=["Yes", "yes", "No"])
            )

    def test_rejects_empty_outcome_label(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=["Yes", "  "]))

    def test_rejects_overlong_outcome_label(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(self.c, outcomes=["Yes", "x" * 81])
            )

    def test_rejects_reserved_fallback_word_none(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=["Yes", "None"]))

    def test_rejects_reserved_fallback_word_unclear_any_case(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=["Yes", "  UNCLEAR. "]))

    def test_rejects_labels_differing_only_by_punctuation(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=["Yes", "Yes.", "No"]))

    def test_rejects_labels_differing_only_by_quotes(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=['"Yes"', "Yes"]))

    def test_rejects_punctuation_only_label(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, outcomes=["Yes", "?!"]))

    def test_internal_whitespace_and_newlines_are_collapsed(self):
        import json
        mid = self.c.create_market(
            **valid_kwargs(self.c, outcomes=["Candidate\n A   wins", "Candidate B wins"])
        )
        record = json.loads(self.c.get_market(mid))
        self.assertEqual(record["outcomes"][0], "Candidate A wins")

    def test_accepts_max_outcomes(self):
        mid = self.c.create_market(
            **valid_kwargs(self.c, outcomes=[f"Option {i}" for i in range(6)])
        )
        self.assertEqual(mid, "0")


class QuestionValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        set_caller(CREATOR_ADDRESS)

    def test_rejects_empty_question(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, question="   "))

    def test_rejects_overlong_question(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, question="x" * 301))


class RequiredSourceDomainsValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        set_caller(CREATOR_ADDRESS)

    def test_rejects_empty_list(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(**valid_kwargs(self.c, required_source_domains=[]))

    def test_rejects_single_domain(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(self.c, required_source_domains=["reuters.com"])
            )

    def test_rejects_unreputable_domain(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(
                    self.c,
                    required_source_domains=["reuters.com", "some-random-blog.xyz"],
                )
            )

    def test_rejects_duplicate_domain(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(
                    self.c,
                    required_source_domains=["reuters.com", "www.reuters.com"],
                )
            )

    def test_accepts_domain_with_path(self):
        mid = self.c.create_market(
            **valid_kwargs(
                self.c,
                required_source_domains=["reuters.com/world", "apnews.com"],
            )
        )
        self.assertEqual(mid, "0")


class TimingValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = make_contract()
        set_caller(CREATOR_ADDRESS)

    def test_rejects_staking_deadline_too_soon(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(self.c, staking_deadline=iso_in(60))
            )

    def test_rejects_staking_deadline_too_far(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(
                    self.c,
                    staking_deadline=iso_in(self.c.MAX_STAKING_LEAD_SECONDS + 60),
                )
            )

    def test_rejects_resolution_gap_too_small(self):
        staking = iso_in(self.c.MIN_STAKING_LEAD_SECONDS + 60)
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(
                    self.c,
                    staking_deadline=staking,
                    resolution_deadline=iso_in(self.c.MIN_STAKING_LEAD_SECONDS + 65),
                )
            )

    def test_rejects_resolution_gap_too_large(self):
        staking = iso_in(self.c.MIN_STAKING_LEAD_SECONDS + 60)
        too_far = iso_in(
            self.c.MIN_STAKING_LEAD_SECONDS
            + 60
            + self.c.MAX_STAKING_TO_RESOLUTION_GAP_SECONDS
            + 60
        )
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(
                    self.c, staking_deadline=staking, resolution_deadline=too_far
                )
            )

    def test_rejects_invalid_timestamp(self):
        with self.assertRaises(gl.vm.UserError):
            self.c.create_market(
                **valid_kwargs(self.c, staking_deadline="not-a-timestamp")
            )


class ListMarketsPaginationTests(unittest.TestCase):
    def setUp(self):
        import json
        self.json = json
        self.c = make_contract()
        set_caller(CREATOR_ADDRESS)
        for i in range(7):
            self.c.create_market(**valid_kwargs(self.c, question=f"Question {i}?"))

    def page(self, offset, limit):
        return self.json.loads(self.c.list_markets(offset, limit))

    def test_newest_first_with_total(self):
        page = self.page(0, 3)
        self.assertEqual(page["total"], 7)
        self.assertEqual([m["market_id"] for m in page["items"]], ["6", "5", "4"])

    def test_offset_and_last_partial_page(self):
        self.assertEqual([m["market_id"] for m in self.page(3, 3)["items"]], ["3", "2", "1"])
        self.assertEqual([m["market_id"] for m in self.page(6, 3)["items"]], ["0"])
        self.assertEqual(self.page(7, 3)["items"], [])
        self.assertEqual(self.page(100, 3)["items"], [])

    def test_limit_capped(self):
        c = make_contract()
        set_caller(CREATOR_ADDRESS)
        for _ in range(c.MAX_PAGE_SIZE + 5):
            c.create_market(**valid_kwargs(c))
        self.assertEqual(len(self.json.loads(c.list_markets(0, 1000))["items"]), c.MAX_PAGE_SIZE)

    def test_invalid_arguments_rejected(self):
        for offset, limit in ((-1, 5), (0, 0), (0, -2), (True, 5), (0, True), ("0", 5)):
            with self.assertRaises(gl.vm.UserError):
                self.c.list_markets(offset, limit)

    def test_empty_contract(self):
        page = self.json.loads(make_contract().list_markets(0, 10))
        self.assertEqual(page, {"total": 0, "items": []})


if __name__ == "__main__":
    unittest.main()
