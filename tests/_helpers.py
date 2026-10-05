"""
Shared helpers for tests that create markets, move time forward and run
resolve_market with mocked web/LLM calls.
"""
import datetime
import json
from contextlib import contextmanager
from unittest.mock import patch

from _bootstrap import CREATOR_ADDRESS, call, gl, set_caller

OUTCOMES = ["Candidate A wins", "Candidate B wins", "Runoff required"]

REUTERS_URL = "https://www.reuters.com/world/election-result"
AP_URL = "https://apnews.com/article/election-result"
BBC_URL = "https://www.bbc.com/news/election-result"
GUARDIAN_URL = "https://www.theguardian.com/world/election-result"
NPR_URL = "https://www.npr.org/election-result"


def iso_in(seconds_from_now: float) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds_from_now)
    return dt.isoformat()


def create_market(c, outcomes=None, required=None):
    set_caller(CREATOR_ADDRESS)
    lead = c.MIN_STAKING_LEAD_SECONDS + 60
    return call(
        c,
        "create_market",
        "Who will win the mayoral election?",
        list(outcomes or OUTCOMES),
        list(required or ["reuters.com", "apnews.com"]),
        iso_in(lead),
        iso_in(lead + c.MIN_STAKING_TO_RESOLUTION_GAP_SECONDS + 60),
    )


def _rewrite(c, mid, **fields):
    record = json.loads(c.markets[mid])
    record.update(fields)
    c.markets[mid] = json.dumps(record, sort_keys=True)


def open_resolution_window(c, mid):
    """Staking closed, resolution window open."""
    _rewrite(
        c, mid,
        staking_deadline=iso_in(-20),
        resolution_deadline=iso_in(-10),
        resolution_window_closes_at=iso_in(c.RESOLUTION_WINDOW_SECONDS),
    )


def close_staking_only(c, mid):
    _rewrite(c, mid, staking_deadline=iso_in(-10))


def close_resolution_window(c, mid):
    _rewrite(
        c, mid,
        staking_deadline=iso_in(-30),
        resolution_deadline=iso_in(-20),
        resolution_window_closes_at=iso_in(-1),
    )


def market(c, mid):
    return json.loads(c.get_market(mid))


# ---------------------------------------------------------------------------
# Mocked sources. Each page states its outcome in a sentence the mocked
# model quotes verbatim, so the quote-grounding check passes.
# ---------------------------------------------------------------------------

A_SENTENCE = "Election officials certified that Candidate A won the mayoral race on Tuesday."
A2_SENTENCE = "Candidate A was sworn in as mayor after the certified result on Tuesday."
B_SENTENCE = "Election officials certified that Candidate B won the mayoral race on Tuesday."
FILLER = " The city council met later in the week to discuss the budget and road repairs."

PAGES = {
    "A": "Local news. " + A_SENTENCE + FILLER,
    "A2": "Regional desk. " + A2_SENTENCE + FILLER,
    "B": "Local news. " + B_SENTENCE + FILLER,
    "POLL": "Local news. A new poll shows Candidate A is expected to win the mayoral race next week." + FILLER,
    "IRRELEVANT": "Local news. The city approved a new park and a bicycle lane downtown this week." + FILLER,
}

ANSWERS = {
    "A": "RELEVANCE: Relevant\nSTATUS: Confirmed\nOUTCOME: Candidate A wins\nQUOTE: " + A_SENTENCE,
    "A2": "RELEVANCE: Relevant\nSTATUS: Confirmed\nOUTCOME: Candidate A wins\nQUOTE: " + A2_SENTENCE,
    "B": "RELEVANCE: Relevant\nSTATUS: Confirmed\nOUTCOME: Candidate B wins\nQUOTE: " + B_SENTENCE,
    "POLL": "RELEVANCE: Relevant\nSTATUS: Projected\nOUTCOME: Candidate A wins\nQUOTE: A new poll shows Candidate A is expected to win",
    "IRRELEVANT": "RELEVANCE: Irrelevant\nSTATUS: Unknown\nOUTCOME: Unclear\nQUOTE: None",
    # Confirmed and relevant, but the quote is fabricated (not on the page).
    "FABRICATED": "RELEVANCE: Relevant\nSTATUS: Confirmed\nOUTCOME: Candidate A wins\nQUOTE: Officials announced a landslide victory for Candidate A today.",
}


@contextmanager
def mocked_sources(page_by_url, answer_override=None, leader_then_validator=None, excerpt_answer="Yes"):
    """
    page_by_url: url -> kind in PAGES (or an Exception instance to raise).
    answer_override: kind -> raw model answer, replacing ANSWERS for that page.
    leader_then_validator: optional list of {url: kind} maps; fetch round i
        (leader = round 0, validator = round 1) uses map i, to simulate a
        validator seeing different content than the leader.
    excerpt_answer: what a validator's model answers to EXCERPT_CHECK.
    """
    answers = dict(ANSWERS)
    if answer_override:
        answers.update(answer_override)
    state = {"calls": 0, "urls_seen": 0, "prompts": []}
    urls = list(page_by_url.keys())

    def render(url, mode="text"):
        mapping = page_by_url
        if leader_then_validator is not None:
            round_index = state["urls_seen"] // len(urls)
            mapping = leader_then_validator[min(round_index, len(leader_then_validator) - 1)]
        state["urls_seen"] += 1
        kind = mapping[url]
        if isinstance(kind, Exception):
            raise kind
        return PAGES[kind]

    def exec_prompt(prompt, response_format="text"):
        state["calls"] += 1
        state["prompts"].append(prompt)
        for kind, page in PAGES.items():
            if page in prompt:
                answer = answers[kind]
                if "EXCERPT_CHECK:" in prompt:
                    answer += "\nEXCERPT_CHECK: " + excerpt_answer
                return answer
        return "RELEVANCE: Unclear\nSTATUS: Unknown\nOUTCOME: Unclear\nQUOTE: None"

    with patch.object(gl.nondet.web, "render", side_effect=render), patch.object(
        gl.nondet, "exec_prompt", side_effect=exec_prompt
    ):
        yield state


def resolve(c, mid, page_by_url, **kwargs):
    with mocked_sources(page_by_url, **kwargs):
        return json.loads(call(c, "resolve_market", mid, list(page_by_url.keys())))
