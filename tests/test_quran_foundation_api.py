import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import quran_foundation_api as qf


def response(status, payload=None, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = payload or {}
    return result


class QuranFoundationAPITests(unittest.TestCase):
    def setUp(self):
        qf.clear_token_cache()
        self.addCleanup(qf.clear_token_cache)

    def test_legacy_endpoint_remains_available_without_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                qf.api_url("chapters"),
                "https://api.quran.com/api/v4/chapters",
            )

    def test_production_endpoint_uses_configured_server_credentials(self):
        with patch.dict(os.environ, {
            "QF_CLIENT_ID": "client-id",
            "QF_CLIENT_SECRET": "client-secret",
            "QF_ENV": "production",
        }, clear=True):
            self.assertEqual(
                qf.api_url("chapters"),
                "https://apis.quran.foundation/content/api/v4/chapters",
            )

    def test_partial_credentials_fail_without_logging_secret(self):
        session = Mock()
        with patch.dict(os.environ, {"QF_CLIENT_ID": "client-id"}, clear=True):
            with self.assertRaisesRegex(qf.QuranFoundationAPIError, "both QF_CLIENT_ID"):
                qf.api_url("chapters")
            session.get.assert_not_called()

    def test_503_respects_retry_after_before_succeeding(self):
        session = Mock()
        session.get.side_effect = [
            response(503, headers={"Retry-After": "5"}),
            response(200, {"chapters": []}),
        ]
        sleeps = []
        with patch.dict(os.environ, {}, clear=True):
            payload = qf.get_json(qf.api_url("chapters"), session=session, sleep=sleeps.append)
        self.assertEqual(payload, {"chapters": []})
        self.assertEqual(sleeps, [5.0])
        self.assertEqual(session.get.call_count, 2)

    def test_525_auth_gateway_failure_is_retried_before_success(self):
        session = Mock()
        session.post.side_effect = [
            response(525),
            response(200, {"access_token": "opaque-token", "expires_in": 3600}),
        ]
        session.get.return_value = response(200, {"chapters": []})
        sleeps = Mock()
        with patch.dict(os.environ, {
            "QF_CLIENT_ID": "client-id",
            "QF_CLIENT_SECRET": "client-secret",
            "QF_ENV": "production",
        }, clear=True), patch("quran_foundation_api.random.uniform", return_value=0):
            payload = qf.get_json(
                qf.api_url("chapters"), session=session, sleep=sleeps
            )
        self.assertEqual(payload, {"chapters": []})
        self.assertEqual(session.post.call_count, 2)
        sleeps.assert_called_once_with(1.0)

    def test_525_content_gateway_failure_is_retried_before_success(self):
        session = Mock()
        session.post.return_value = response(
            200, {"access_token": "opaque-token", "expires_in": 3600}
        )
        session.get.side_effect = [response(525), response(200, {"chapters": []})]
        sleeps = Mock()
        with patch.dict(os.environ, {
            "QF_CLIENT_ID": "client-id",
            "QF_CLIENT_SECRET": "client-secret",
            "QF_ENV": "production",
        }, clear=True), patch("quran_foundation_api.random.uniform", return_value=0):
            payload = qf.get_json(
                qf.api_url("chapters"), session=session, sleep=sleeps
            )
        self.assertEqual(payload, {"chapters": []})
        self.assertEqual(session.get.call_count, 2)
        sleeps.assert_called_once_with(1.0)

    def test_http_date_retry_after_is_parsed_and_bounded(self):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        delay = qf._retry_delay(
            response(503, headers={"Retry-After": "Mon, 28 Sep 2026 12:00:12 GMT"}),
            1,
            now=now,
        )
        self.assertEqual(delay, 12.0)
        self.assertEqual(
            qf._retry_delay(response(503, headers={"Retry-After": "100"}), 1),
            qf.MAX_RETRY_AFTER_SECONDS,
        )

    def test_client_credentials_token_is_cached_and_never_put_in_url(self):
        session = Mock()
        session.post.return_value = response(200, {"access_token": "opaque-token", "expires_in": 3600})
        session.get.return_value = response(200, {"chapters": []})
        with patch.dict(os.environ, {
            "QF_CLIENT_ID": "client-id",
            "QF_CLIENT_SECRET": "client-secret",
            "QF_ENV": "production",
        }, clear=True):
            url = qf.api_url("chapters")
            qf.get_json(url, session=session, sleep=Mock())
            qf.get_json(url, session=session, sleep=Mock())
        session.post.assert_called_once()
        self.assertEqual(session.post.call_args.kwargs["auth"], ("client-id", "client-secret"))
        self.assertEqual(session.post.call_args.kwargs["data"], {
            "grant_type": "client_credentials", "scope": "content"
        })
        headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["x-client-id"], "client-id")
        self.assertEqual(headers["x-auth-token"], "opaque-token")
        self.assertNotIn("client-secret", url)

    def test_401_refreshes_token_exactly_once(self):
        session = Mock()
        session.post.side_effect = [
            response(200, {"access_token": "old-token", "expires_in": 3600}),
            response(200, {"access_token": "new-token", "expires_in": 3600}),
        ]
        session.get.side_effect = [response(401), response(200, {"ok": True})]
        with patch.dict(os.environ, {
            "QF_CLIENT_ID": "client-id",
            "QF_CLIENT_SECRET": "client-secret",
            "QF_ENV": "production",
        }, clear=True):
            result = qf.get_json(qf.api_url("chapters"), session=session, sleep=Mock())
        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs["headers"]["x-auth-token"], "new-token")

    def test_permanent_client_error_is_not_retried(self):
        session = Mock()
        session.get.return_value = response(403)
        sleeps = Mock()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(qf.QuranFoundationAPIError, "HTTP 403"):
                qf.get_json(qf.api_url("chapters"), session=session, sleep=sleeps)
        self.assertEqual(session.get.call_count, 1)
        sleeps.assert_not_called()

    def test_credentials_are_never_sent_to_a_non_qf_host(self):
        session = Mock()
        with patch.dict(os.environ, {
            "QF_CLIENT_ID": "client-id",
            "QF_CLIENT_SECRET": "client-secret",
            "QF_ENV": "production",
        }, clear=True):
            with self.assertRaisesRegex(qf.QuranFoundationAPIError, "untrusted host"):
                qf.get_json("https://example.com/chapters", session=session)
        session.post.assert_not_called()
        session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
