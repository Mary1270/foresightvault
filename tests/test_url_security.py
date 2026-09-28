"""
Strict source-URL parsing. A URL that could be fetched from a different
host than the one checked against the allowlist must be rejected outright.
"""
import unittest

from _bootstrap import M, gl, make_contract, ALICE_ADDRESS, call_payable, set_caller, call
from _helpers import AP_URL, REUTERS_URL, create_market, open_resolution_window


class ParseSourceUrlTests(unittest.TestCase):
    def assertRejected(self, url):
        self.assertEqual(M.parse_source_url(url), ("", ""), url)

    def test_plain_https_urls_parse(self):
        self.assertEqual(M.parse_source_url("https://www.reuters.com/World/x/"), ("reuters.com", "/world/x"))
        self.assertEqual(M.parse_source_url("https://apnews.com"), ("apnews.com", ""))
        self.assertEqual(M.parse_source_url("https://news.bbc.co.uk/a"), ("bbc.co.uk", "/a"))

    def test_explicit_default_port_allowed(self):
        self.assertEqual(M.parse_source_url("https://reuters.com:443/x"), ("reuters.com", "/x"))

    def test_backslash_host_confusion_rejected(self):
        # A WHATWG parser fetches this from attacker.example.
        self.assertRejected("https://attacker.example\\@reuters.com/x")
        self.assertRejected("https://reuters.com\\.attacker.example/x")

    def test_embedded_credentials_rejected(self):
        self.assertRejected("https://reuters.com:443@attacker.example/x")
        self.assertRejected("https://user:pass@reuters.com/x")
        self.assertRejected("https://reuters.com@attacker.example/")

    def test_non_https_schemes_rejected(self):
        for url in ("http://reuters.com/x", "ftp://reuters.com/x", "javascript:alert(1)", "reuters.com/x"):
            self.assertRejected(url)

    def test_non_default_ports_rejected(self):
        self.assertRejected("https://reuters.com:8443/x")
        self.assertRejected("https://reuters.com:99999/x")

    def test_whitespace_and_control_characters_rejected(self):
        self.assertRejected("https://reuters.com/a b")
        self.assertRejected("https://reuters.com/\tx")
        self.assertRejected("https://reuters.com/\x00x")

    def test_ip_and_malformed_hosts_rejected(self):
        for url in ("https://127.0.0.1/x", "https://[::1]/x", "https://-reuters.com/x",
                    "https://reuters..com/x", "https://localhost/x", "https:///x"):
            self.assertRejected(url)

    def test_non_string_and_overlong_rejected(self):
        self.assertRejected(None)
        self.assertRejected(123)
        self.assertRejected("https://reuters.com/" + "a" * 3000)

    def test_lookalike_domains_do_not_match_allowlist(self):
        domain, _ = M.parse_source_url("https://reuters.com.attacker.example/x")
        self.assertEqual(domain, "attacker.example")
        domain, _ = M.parse_source_url("https://notreuters.com/x")
        self.assertNotIn(domain, M.REPUTABLE_NEWS_DOMAINS)


class PathTraversalTests(unittest.TestCase):
    """A fetcher normalizes dot segments and backslashes, so these would
    escape a committed path prefix if accepted."""

    def test_dot_segments_rejected(self):
        for url in ("https://reuters.com/world/../sport", "https://reuters.com/world/./x",
                    "https://reuters.com/world/..", "https://reuters.com/./world"):
            self.assertEqual(M.parse_source_url(url), ("", ""), url)

    def test_encoded_dot_segments_and_separators_rejected(self):
        for url in ("https://reuters.com/world/%2e%2e/sport", "https://reuters.com/world/%2E%2E/sport",
                    "https://reuters.com/world%2f..%2fsport", "https://reuters.com/world%5csport"):
            self.assertEqual(M.parse_source_url(url), ("", ""), url)

    def test_backslash_in_path_rejected(self):
        self.assertEqual(M.parse_source_url("https://reuters.com/world/..\\sport"), ("", ""))
        self.assertEqual(M.parse_source_url("https://reuters.com/world\\x"), ("", ""))

    def test_ordinary_dots_in_segments_still_allowed(self):
        self.assertEqual(M.parse_source_url("https://reuters.com/world/v1.2/story.html"),
                         ("reuters.com", "/world/v1.2/story.html"))


class PathPrefixTests(unittest.TestCase):
    def test_segment_aware_prefix(self):
        self.assertTrue(M.path_matches("/world", "/world"))
        self.assertTrue(M.path_matches("/world/europe", "/world"))
        self.assertFalse(M.path_matches("/worldcup", "/world"))
        self.assertTrue(M.path_matches("/anything", ""))


class ResolveRejectsBadUrlsBeforeFetchTests(unittest.TestCase):
    """Bad URLs are rejected by resolve_market itself, before any network
    access (render is not even patched here; a fetch would raise)."""

    def setUp(self):
        self.c = make_contract()
        self.mid = create_market(self.c)
        open_resolution_window(self.c, self.mid)

    def test_host_confusion_url_rejected(self):
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "resolve_market", self.mid,
                 ["https://attacker.example\\@reuters.com/x", AP_URL])

    def test_non_allowlisted_extra_source_rejected(self):
        with self.assertRaises(gl.vm.UserError):
            call(self.c, "resolve_market", self.mid, [REUTERS_URL, AP_URL, "https://example.org/x"])

    def test_committed_path_prefix_not_satisfied_by_lookalike_path(self):
        c = make_contract()
        mid = create_market(c, required=["reuters.com/world", "apnews.com"])
        open_resolution_window(c, mid)
        with self.assertRaises(gl.vm.UserError):
            call(c, "resolve_market", mid, ["https://reuters.com/worldcup/final", AP_URL])

    def test_required_domain_entry_with_credentials_rejected_at_creation(self):
        c = make_contract()
        with self.assertRaises(gl.vm.UserError):
            create_market(c, required=["https://x@reuters.com", "apnews.com"])


if __name__ == "__main__":
    unittest.main()
