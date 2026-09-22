import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch


# The safety helpers are pure Python, but their module imports optional runtime
# dependencies used by the real scraper.  Stub those dependencies so this test
# suite stays runnable in a minimal environment.
instagrapi = types.ModuleType("instagrapi")
instagrapi.__path__ = []


class StubClient:
    pass


class StubLoginRequired(Exception):
    pass


class StubUserNotFound(Exception):
    pass


class StubClientError(Exception):
    pass


instagrapi.Client = StubClient
instagrapi_extractors = types.ModuleType("instagrapi.extractors")
instagrapi_extractors.extract_media_gql = lambda value: value
instagrapi_extractors.extract_media_v1 = lambda value: value
instagrapi_exceptions = types.ModuleType("instagrapi.exceptions")
instagrapi_exceptions.LoginRequired = StubLoginRequired
instagrapi_exceptions.UserNotFound = StubUserNotFound
instagrapi_exceptions.ClientError = StubClientError

playwright = types.ModuleType("playwright")
playwright.__path__ = []
playwright_sync_api = types.ModuleType("playwright.sync_api")
playwright_sync_api.sync_playwright = lambda: None

sys.modules["instagrapi"] = instagrapi
sys.modules["instagrapi.extractors"] = instagrapi_extractors
sys.modules["instagrapi.exceptions"] = instagrapi_exceptions
sys.modules["playwright"] = playwright
sys.modules["playwright.sync_api"] = playwright_sync_api

from core import scraper_instagram as scraper


class LikerCompletenessTests(unittest.TestCase):
    def make_result(
        self,
        usernames=(),
        user_ids=(),
        *,
        expected_count=None,
        reached_end=False,
        connection_total=None,
    ):
        return scraper._liker_result_from_identities(
            set(usernames),
            set(user_ids),
            source="test",
            expected_count=expected_count,
            reached_end=reached_end,
            connection_total=connection_total,
        )

    def test_missing_result_without_end_proof_is_unavailable(self):
        result = self.make_result(expected_count=0)

        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.complete)
        self.assertEqual(result.observed_count, 0)

    def test_partial_identities_without_end_proof_are_never_complete(self):
        result = self.make_result(
            usernames={"alice"},
            expected_count=2,
            connection_total=2,
        )

        self.assertEqual(result.status, "partial")
        self.assertFalse(result.complete)

    def test_expected_media_count_match_does_not_prove_completeness(self):
        result = self.make_result(
            usernames={"alice", "bob"},
            expected_count=2,
        )

        self.assertEqual(result.observed_count, result.expected_count)
        self.assertEqual(result.status, "partial")
        self.assertFalse(result.complete)

    def test_reached_end_with_matching_connection_total_is_complete(self):
        result = self.make_result(
            usernames={"alice", "bob"},
            expected_count=99,
            reached_end=True,
            connection_total=2,
        )

        self.assertEqual(result.status, "complete")
        self.assertTrue(result.complete)
        self.assertTrue(result.usernames_complete)
        self.assertFalse(result.user_ids_complete)

    def test_reached_end_without_connection_total_is_not_complete(self):
        result = self.make_result(
            usernames={"alice"},
            reached_end=True,
            connection_total=None,
        )

        self.assertEqual(result.status, "partial")
        self.assertFalse(result.complete)

    def test_reached_end_with_incomplete_connection_payload_stays_partial(self):
        result = self.make_result(
            usernames={"alice"},
            reached_end=True,
            connection_total=2,
        )

        self.assertEqual(result.status, "partial")
        self.assertFalse(result.complete)


class ExceptionClassificationTests(unittest.TestCase):
    class HttpError(RuntimeError):
        def __init__(self, status_code):
            super().__init__(f"HTTP {status_code}")
            self.response = SimpleNamespace(status_code=status_code)

    def test_rate_limit_is_detected_from_status_or_instagram_marker(self):
        self.assertEqual(
            scraper._classify_instagram_exception(self.HttpError(429)),
            "rate_limited",
        )
        self.assertEqual(
            scraper._classify_instagram_exception(RuntimeError("feedback_required")),
            "rate_limited",
        )

        login_error = StubLoginRequired("login request throttled")
        login_error.response = SimpleNamespace(status_code=429)
        self.assertEqual(
            scraper._classify_instagram_exception(login_error),
            "rate_limited",
        )

        class PleaseWaitFewMinutes(RuntimeError):
            pass

        self.assertEqual(
            scraper._classify_instagram_exception(PleaseWaitFewMinutes()),
            "rate_limited",
        )

    def test_authentication_errors_are_classified_as_unauthenticated(self):
        self.assertEqual(
            scraper._classify_instagram_exception(StubLoginRequired("login required")),
            "unauthenticated",
        )
        self.assertEqual(
            scraper._classify_instagram_exception(self.HttpError(403)),
            "unauthenticated",
        )
        self.assertEqual(
            scraper._classify_instagram_exception(RuntimeError("challenge_required")),
            "unauthenticated",
        )

    def test_ordinary_failure_remains_non_terminal_error(self):
        self.assertEqual(
            scraper._classify_instagram_exception(RuntimeError("invalid payload")),
            "error",
        )

    def test_custom_terminal_errors_keep_their_category(self):
        self.assertEqual(
            scraper._classify_instagram_exception(scraper.InstagramRateLimitError("dibatasi")),
            "rate_limited",
        )
        self.assertEqual(
            scraper._classify_instagram_exception(scraper.InstagramAuthenticationError("sesi habis")),
            "unauthenticated",
        )


class OptionalCountTests(unittest.TestCase):
    def test_missing_count_stays_none(self):
        self.assertIsNone(scraper._optional_count(None))

    def test_explicit_zero_stays_zero(self):
        self.assertEqual(scraper._optional_count(0), 0)
        self.assertIsNotNone(scraper._optional_count(0))

    def test_abbreviated_social_counts_are_scaled(self):
        self.assertEqual(scraper._optional_count("1.2K"), 1_200)
        self.assertEqual(scraper._optional_count("1,5 rb"), 1_500)
        self.assertEqual(scraper._optional_count("2 jt"), 2_000_000)


class CommentPayloadExtractionTests(unittest.TestCase):
    def test_extracts_and_deduplicates_comment_nodes_from_dynamic_connection(self):
        node = {
            "id": "comment-1",
            "text": "halo",
            "owner": {"id": "user-1", "username": "alice"},
            "created_at": 1_700_000_000,
            "like_count": 2,
        }
        payload = {
            "data": {
                "xdt_new_comment_connection_name": {
                    "edges": [{"node": node}, {"node": dict(node)}],
                    "page_info": {"has_next_page": False},
                }
            }
        }

        comments, reached_end = scraper._comment_nodes_from_payload(payload)

        self.assertEqual(comments, [node])
        self.assertTrue(reached_end)

    def test_unrelated_page_info_does_not_prove_comment_pagination_complete(self):
        payload = {
            "data": {
                "feed_connection": {
                    "page_info": {"has_next_page": False},
                    "edges": [],
                }
            }
        }

        comments, reached_end = scraper._comment_nodes_from_payload(payload)

        self.assertEqual(comments, [])
        self.assertFalse(reached_end)


class OEmbedSafetyTests(unittest.TestCase):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"title": "caption"}

    def test_caption_enrichment_has_a_per_job_limit(self):
        calls = []
        client = SimpleNamespace(
            public=SimpleNamespace(
                get=lambda *args, **kwargs: calls.append(args) or self.Response(),
            ),
            public_user_agent="test",
            request_timeout=20,
        )
        reels = [SimpleNamespace(code=str(index), caption_text="") for index in range(12)]
        messages = []

        with patch.object(scraper.time, "sleep"):
            scraper._enrich_reel_captions_oembed(client, reels, messages.append)

        self.assertEqual(len(calls), scraper.OEMBED_CAPTION_ENRICH_LIMIT)
        self.assertIn("2 dilewati", messages[-1])

    def test_rate_limit_stops_oembed_enrichment(self):
        class Http429(RuntimeError):
            response = SimpleNamespace(status_code=429)

        client = SimpleNamespace(
            public=SimpleNamespace(get=lambda *args, **kwargs: (_ for _ in ()).throw(Http429("limited"))),
            public_user_agent="test",
            request_timeout=20,
        )

        with self.assertRaises(scraper.InstagramRateLimitError):
            scraper._enrich_reel_captions_oembed(
                client,
                [SimpleNamespace(code="one", caption_text="")],
            )


class SessionIdentityTests(unittest.TestCase):
    class CookieJar(list):
        def set(self, name, value):
            self[:] = [cookie for cookie in self if cookie.name != name]
            self.append(SimpleNamespace(name=name, value=value))

    def make_client(self, viewer):
        private_cookies = self.CookieJar()
        public_cookies = self.CookieJar()
        return SimpleNamespace(
            sessionid="12345%3Asecret",
            authorization_data={},
            private=SimpleNamespace(cookies=private_cookies),
            public=SimpleNamespace(cookies=public_cookies),
            public_doc_id_graphql_request=lambda *args, **kwargs: {
                "xdt_viewer": {"user": viewer}
            },
        )

    def test_session_must_return_an_authenticated_viewer(self):
        client = self.make_client(None)

        self.assertFalse(scraper._validate_web_session(client))

    def test_session_owner_and_expected_username_must_match(self):
        matching = self.make_client({"pk": "12345", "username": "alice"})
        self.assertTrue(scraper._validate_web_session(matching, expected_username="@Alice"))

        wrong_account = self.make_client({"pk": "12345", "username": "bob"})
        with self.assertRaises(scraper.InstagramAuthenticationError):
            scraper._validate_web_session(wrong_account, expected_username="alice")

    def test_sessionid_owner_prefix_must_match_viewer_id(self):
        client = self.make_client({"pk": "99999", "username": "alice"})

        self.assertFalse(scraper._validate_web_session(client, expected_username="alice"))

    def test_expected_username_requires_username_in_viewer_payload(self):
        client = self.make_client({"pk": "12345"})

        self.assertFalse(scraper._validate_web_session(client, expected_username="alice"))

    def make_web_form_client(self, payload, status_code=200):
        client = self.make_client(None)

        class Response:
            def json(self):
                return payload

        response = Response()
        response.status_code = status_code
        client.public.get = lambda *args, **kwargs: response
        client.public_user_agent = "test-agent"
        client.request_timeout = 20
        client.public_doc_id_graphql_request = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GraphQL fallback tidak boleh dipanggil untuk hasil definitif")
        )
        return client

    def test_account_form_can_validate_active_session_without_xdt_viewer(self):
        client = self.make_web_form_client(
            {"form_data": {"username": "alice"}}
        )

        self.assertTrue(scraper._validate_web_session(client, expected_username="alice"))
        self.assertEqual(
            client._instagram_viewer_identity,
            {"id": "12345", "username": "alice"},
        )

    def test_account_form_username_must_match_login_account(self):
        client = self.make_web_form_client(
            {"form_data": {"username": "bob"}}
        )

        with self.assertRaises(scraper.InstagramAuthenticationError):
            scraper._validate_web_session(client, expected_username="alice")

    def test_account_form_login_required_is_definitive(self):
        client = self.make_web_form_client(
            {"message": "login_required", "status": "fail"}
        )

        self.assertFalse(scraper._validate_web_session(client, expected_username="alice"))


class ZeroPostLikeShortcutTests(unittest.TestCase):
    class RecordingClient:
        def __init__(self):
            self.media_likers_calls = []

        def media_likers(self, media_id):
            self.media_likers_calls.append(media_id)
            return []

    def test_visible_zero_skips_liker_request_but_does_not_prove_complete(self):
        client = self.RecordingClient()
        media = SimpleNamespace(
            id="123_456",
            pk="123",
            like_and_view_counts_disabled=False,
        )

        result = scraper._get_media_liker_identities(client, media, post_likes=0)

        self.assertEqual(client.media_likers_calls, [])
        self.assertEqual(result.source, "post_like_count")
        self.assertEqual(result.status, "not_checked")
        self.assertFalse(result.complete)
        self.assertEqual(result.expected_count, 0)

    def test_hidden_zero_does_not_use_visible_count_shortcut(self):
        client = self.RecordingClient()
        media = SimpleNamespace(
            id="123_456",
            pk="123",
            like_and_view_counts_disabled=True,
        )

        result = scraper._get_media_liker_identities(client, media, post_likes=0)

        self.assertEqual(client.media_likers_calls, ["123_456"])
        self.assertEqual(result.source, "private_api")
        self.assertFalse(result.complete)


class CommentResultSafetyTests(unittest.TestCase):
    @staticmethod
    def media(**overrides):
        values = {
            "id": "123_456",
            "pk": "123",
            "code": "ABC123",
            "caption_text": "caption",
            "like_count": 2,
            "comment_count": 2,
            "taken_at": datetime(2026, 9, 20, 10, 0, 0),
            "product_type": "feed",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_partial_liker_list_proves_yes_but_never_no_and_keeps_missing_comment_likes(self):
        comments = [
            {
                "user": {"username": "alice", "pk": "1"},
                "text": "matched",
                "created_at": 1_700_000_000,
            },
            {
                "user": {"username": "bob", "pk": "2"},
                "text": "not observed",
                "comment_like_count": 0,
                "created_at": 1_700_000_001,
            },
        ]
        client = SimpleNamespace(
            media_comments_gql=lambda media_id, amount: comments,
            media_comments=lambda media_id, amount: [],
        )
        liker_result = scraper.LikerLookupResult(
            usernames={"alice"},
            status="partial",
            source="test",
            reason="pagination belum selesai",
        )

        with patch.object(scraper, "_get_media_liker_identities", return_value=liker_result):
            result = scraper.get_comments_from_post(client, self.media())

        self.assertEqual(result[0]["has_liked_post"], "Ya")
        self.assertIsNone(result[0]["comment_likes"])
        self.assertEqual(result[1]["has_liked_post"], "Belum dapat diverifikasi")
        self.assertEqual(result[1]["comment_likes"], 0)
        self.assertFalse(result[1]["liker_lookup_complete"])

    def test_complete_liker_list_can_prove_negative(self):
        comments = [{"user": {"username": "bob", "pk": "2"}, "text": "hello"}]
        client = SimpleNamespace(
            media_comments_gql=lambda media_id, amount: comments,
            media_comments=lambda media_id, amount: [],
        )
        liker_result = scraper.LikerLookupResult(
            usernames={"alice"},
            status="complete",
            source="test",
            usernames_complete=True,
        )

        with patch.object(scraper, "_get_media_liker_identities", return_value=liker_result):
            result = scraper.get_comments_from_post(
                client,
                self.media(comment_count=1),
            )

        self.assertEqual(result[0]["has_liked_post"], "Tidak")

    def test_complete_user_id_namespace_does_not_prove_username_negative(self):
        comments = [{"user": {"username": "bob"}, "text": "hello"}]
        client = SimpleNamespace(media_comments_gql=lambda *_args, **_kwargs: comments)
        liker_result = scraper.LikerLookupResult(
            user_ids={"1"},
            status="complete",
            source="test",
            user_ids_complete=True,
        )

        with patch.object(scraper, "_get_media_liker_identities", return_value=liker_result):
            result = scraper.get_comments_from_post(client, self.media(comment_count=1))

        self.assertEqual(result[0]["has_liked_post"], "Belum dapat diverifikasi")

    def test_complete_username_namespace_does_not_prove_user_id_negative(self):
        comments = [{"user": {"id": "2"}, "text": "hello"}]
        client = SimpleNamespace(media_comments_gql=lambda *_args, **_kwargs: comments)
        liker_result = scraper.LikerLookupResult(
            usernames={"alice"},
            status="complete",
            source="test",
            usernames_complete=True,
        )

        with patch.object(scraper, "_get_media_liker_identities", return_value=liker_result):
            result = scraper.get_comments_from_post(client, self.media(comment_count=1))

        self.assertEqual(result[0]["has_liked_post"], "Belum dapat diverifikasi")

    def test_complete_liker_list_does_not_guess_when_commenter_identity_is_missing(self):
        comments = [{"user": {}, "text": "anonymous payload"}]
        client = SimpleNamespace(
            media_comments_gql=lambda media_id, amount: comments,
            media_comments=lambda media_id, amount: [],
        )
        liker_result = scraper.LikerLookupResult(status="complete", source="test")

        with patch.object(scraper, "_get_media_liker_identities", return_value=liker_result):
            result = scraper.get_comments_from_post(
                client,
                self.media(comment_count=1),
            )

        self.assertEqual(result[0]["has_liked_post"], "Belum dapat diverifikasi")

    def test_rate_limit_stops_comment_fallback_immediately(self):
        calls = []

        class Http429(RuntimeError):
            response = SimpleNamespace(status_code=429)

        def gql(media_id, amount):
            calls.append("graphql")
            raise Http429("limited")

        def private(media_id, amount):
            calls.append("private")
            return []

        client = SimpleNamespace(media_comments_gql=gql, media_comments=private)

        with self.assertRaises(scraper.InstagramRateLimitError):
            scraper.get_comments_from_post(client, self.media())

        self.assertEqual(calls, ["graphql"])

    def test_empty_endpoints_do_not_override_positive_comment_metadata(self):
        client = SimpleNamespace(
            media_comments_gql=lambda media_id, amount: [],
            media_comments=lambda media_id, amount: [],
        )

        with self.assertRaises(scraper.InstagramCommentFetchError):
            scraper.get_comments_from_post(client, self.media(comment_count=4))

    def test_web_session_never_falls_back_to_private_comment_endpoint(self):
        calls = []
        client = SimpleNamespace(
            _instagram_web_session=True,
            media_comments_gql=lambda *_args, **_kwargs: calls.append("graphql") or [],
            media_comments=lambda *_args, **_kwargs: calls.append("private") or [],
        )

        result = scraper.get_comments_from_post(client, self.media(comment_count=0))

        self.assertEqual(result, [])
        self.assertEqual(calls, ["graphql"])

    def test_web_session_empty_comments_with_positive_metadata_is_reported(self):
        calls = []
        client = SimpleNamespace(
            _instagram_web_session=True,
            media_comments_gql=lambda *_args, **_kwargs: calls.append("graphql") or [],
            media_comments=lambda *_args, **_kwargs: calls.append("private") or [],
        )

        with self.assertRaises(scraper.InstagramCommentFetchError):
            scraper.get_comments_from_post(client, self.media(comment_count=3))

        self.assertEqual(calls, ["graphql"])

    def test_web_session_uses_browser_comment_fallback_after_graphql_decode_failure(self):
        calls = []

        def gql(*_args, **_kwargs):
            calls.append("graphql")
            raise RuntimeError("ClientJSONDecodeError")

        browser = SimpleNamespace(
            lookup_comments=lambda *_args, **_kwargs: scraper.CommentLookupResult(
                comments=[
                    {
                        "id": "comment-1",
                        "user": {"username": "alice", "id": "user-1"},
                        "text": "berhasil dari browser",
                        "created_at": 1_700_000_000,
                    }
                ],
                status="complete",
                source="browser_web",
                expected_count=1,
                reached_end=True,
            )
        )
        client = SimpleNamespace(
            _instagram_web_session=True,
            media_comments_gql=gql,
            media_comments=lambda *_args, **_kwargs: calls.append("private") or [],
        )

        result = scraper.get_comments_from_post(
            client,
            self.media(comment_count=1),
            fetch_likers=False,
            liker_browser=browser,
        )

        self.assertEqual(calls, ["graphql"])
        self.assertEqual(result[0]["comment_text"], "berhasil dari browser")
        self.assertEqual(client._last_comment_lookup["source"], "browser_web")

    def test_comment_cap_is_exposed_as_partial_diagnostics(self):
        comments = [
            {"user": {"username": f"user{index}"}, "text": "hello"}
            for index in range(scraper.COMMENTS_PER_POST_LIMIT)
        ]
        client = SimpleNamespace(media_comments_gql=lambda *_args, **_kwargs: comments)
        with patch.object(
            scraper,
            "_get_media_liker_identities",
            return_value=scraper.LikerLookupResult(status="not_checked", source="test"),
        ):
            result = scraper.get_comments_from_post(
                client,
                self.media(comment_count=scraper.COMMENTS_PER_POST_LIMIT + 25),
            )

        self.assertEqual(len(result), scraper.COMMENTS_PER_POST_LIMIT)
        self.assertEqual(client._last_comment_lookup["status"], "partial")
        self.assertFalse(client._last_comment_lookup["complete"])


class JobCircuitBreakerTests(unittest.TestCase):
    @staticmethod
    def posts(count):
        return [
            SimpleNamespace(code=f"post-{index}", pk=str(index), taken_at=None)
            for index in range(count)
        ]

    def test_comment_failures_mark_partial_but_all_posts_are_considered(self):
        client = SimpleNamespace(_instagram_web_session=False)
        failure = scraper.InstagramCommentFetchError("endpoint komentar gagal")

        with patch.object(scraper, "get_comments_from_post", side_effect=failure) as mocked:
            result = scraper.get_all_comments(client, self.posts(5))

        self.assertEqual(result, [])
        self.assertEqual(mocked.call_count, 5)
        diagnostics = client._instagram_job_diagnostics
        self.assertTrue(diagnostics["comment_circuit_open"])
        self.assertEqual(len(diagnostics["comment_errors"]), 5)

    def test_liker_failures_open_circuit_but_comments_continue(self):
        client = SimpleNamespace(_instagram_web_session=False)
        fetch_flags = []

        def fake_comments(client_arg, media, fetch_likers=True, **kwargs):
            fetch_flags.append(fetch_likers)
            lookup = scraper.LikerLookupResult(
                status="unavailable" if fetch_likers else "not_checked",
                source="test" if fetch_likers else "circuit_breaker",
                reason="tidak tersedia",
            )
            client_arg._last_liker_lookup = lookup.diagnostics()
            return []

        with patch.object(scraper, "get_comments_from_post", side_effect=fake_comments):
            result = scraper.get_all_comments(client, self.posts(5))

        self.assertEqual(result, [])
        self.assertEqual(fetch_flags, [True, True, True, False, False])
        self.assertTrue(client._instagram_job_diagnostics["liker_circuit_open"])

    def test_global_liker_budget_stops_partial_lookups(self):
        client = SimpleNamespace(_instagram_web_session=False)
        fetch_flags = []

        def fake_comments(client_arg, media, fetch_likers=True, **kwargs):
            fetch_flags.append(fetch_likers)
            lookup = scraper.LikerLookupResult(
                usernames={"known"} if fetch_likers else set(),
                status="partial" if fetch_likers else "not_checked",
                source="test" if fetch_likers else "circuit_breaker",
                reason="pagination dibatasi",
            )
            client_arg._last_liker_lookup = lookup.diagnostics()
            client_arg._last_comment_lookup = {"status": "complete"}
            return []

        post_count = scraper.LIKER_LOOKUP_JOB_LIMIT + 2
        with patch.object(scraper, "get_comments_from_post", side_effect=fake_comments):
            result = scraper.get_all_comments(client, self.posts(post_count))

        self.assertEqual(result, [])
        self.assertEqual(fetch_flags[:scraper.LIKER_LOOKUP_JOB_LIMIT], [True] * scraper.LIKER_LOOKUP_JOB_LIMIT)
        self.assertEqual(fetch_flags[scraper.LIKER_LOOKUP_JOB_LIMIT:], [False, False])
        diagnostics = client._instagram_job_diagnostics
        self.assertTrue(diagnostics["liker_circuit_open"])
        self.assertEqual(diagnostics["liker_lookup_attempts"], scraper.LIKER_LOOKUP_JOB_LIMIT)


class BrowserSafetyHelpersTests(unittest.TestCase):
    def test_security_redirects_are_terminal(self):
        helper = scraper._InstagramLikerBrowser._security_redirect_reason

        self.assertIn("challenge", helper("https://www.instagram.com/challenge/123/").lower())
        self.assertIn("checkpoint", helper("https://www.instagram.com/checkpoint/abc/").lower())
        self.assertIn("login", helper("https://www.instagram.com/accounts/login/").lower())
        self.assertEqual(helper("https://www.instagram.com/p/ABC/"), "")


if __name__ == "__main__":
    unittest.main()
