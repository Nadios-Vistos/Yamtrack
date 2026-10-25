from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from app.providers import services


def rate_limited(retry_after):
    """Return a 429 response asking to wait."""
    response = MagicMock(status_code=429, headers={"Retry-After": retry_after})
    response.raise_for_status.side_effect = requests.exceptions.HTTPError(
        response=response,
    )
    return response


class RateLimitTests(SimpleTestCase):
    """A provider asking to wait for long must not block the request."""

    @patch("app.providers.services.time.sleep")
    @patch("app.providers.services.session")
    def test_long_wait_gives_up(self, session, sleep):
        """A Retry-After of hours raises at once instead of sleeping."""
        session.get.return_value = rate_limited("6308")
        with self.assertRaises(requests.exceptions.HTTPError):
            services.api_request("HARDCOVER", "GET", "https://example.invalid")
        sleep.assert_not_called()

    @patch("app.providers.services.time.sleep")
    @patch("app.providers.services.session")
    def test_short_wait_retries(self, session, sleep):
        """A short Retry-After still waits and retries."""
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"ok": True}
        session.get.side_effect = [rate_limited("2"), ok]
        self.assertEqual(
            services.api_request("TMDB", "GET", "https://example.invalid"),
            {"ok": True},
        )
        sleep.assert_called_once_with(5)
