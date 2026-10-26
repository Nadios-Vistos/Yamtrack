from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse

from app.models import Status
from sosoblue import genres, series
from sosoblue.models import SeriesStatus
from sosoblue.tests.test_series import SeriesTestCase, tv_metadata


class AnimationFilterTests(SeriesTestCase):
    """Filtering the TV list on animation."""

    def setUp(self):
        """Two shows, one animated, and a logged in user."""
        super().setUp()
        cache.clear()
        self.cartoon, _ = self.make_show("1", Status.COMPLETED.value, "Cartoon")
        self.drama, _ = self.make_show("2", Status.COMPLETED.value, "Drama")
        self.client.force_login(self.user)

    def refresh(self):
        """Fetch the genres of both shows."""

        def fake_tv(media_id):
            data = tv_metadata("Ended")
            data["genres"] = ["Animation", "Comédie"] if media_id == "1" else ["Drame"]
            return data

        with patch("sosoblue.series.tmdb.tv", side_effect=fake_tv):
            series.refresh_series_status()

    def titles(self, **params):
        """Return the titles of the TV list."""
        url = reverse("medialist", args=[self.user.username, "tv"])
        response = self.client.get(url, params)
        return sorted(media.item.title for media in response.context["media_list"])

    def test_refresh_stores_genres(self):
        """The refresh keeps the genres and flags animation."""
        self.refresh()
        self.assertTrue(SeriesStatus.objects.get(item=self.cartoon).is_animation)
        status = SeriesStatus.objects.get(item=self.drama)
        self.assertFalse(status.is_animation)
        self.assertEqual(status.genres, ["Drame"])
        self.assertEqual(genres.missing_count(), 0)

    def test_filter(self):
        """The filter is applied and remembered for the next pages."""
        self.refresh()
        self.assertEqual(self.titles(genre="animation"), ["Cartoon"])
        self.assertEqual(self.titles(), ["Cartoon"])
        self.assertEqual(self.titles(genre="live"), ["Drama"])
        self.assertEqual(self.titles(genre="all"), ["Cartoon", "Drama"])

    def test_filter_only_on_tv(self):
        """Other lists ignore it."""
        self.refresh()
        self.titles(genre="animation")
        url = reverse("medialist", args=[self.user.username, "movie"])
        response = self.client.get(url)
        self.assertNotContains(response, "sb-genre-filter")

    @patch("sosoblue.tasks.refresh_series_status.delay")
    def test_backfill(self, mock_delay):
        """Missing genres are fetched in the background, once an hour."""
        url = reverse("medialist", args=[self.user.username, "tv"])
        response = self.client.get(url, {"genre": "animation"})
        self.assertTrue(response.context["sb_filter"]["missing"])
        response = self.client.get(url, {"genre": "animation"})
        self.assertFalse(response.context["sb_filter"]["missing"])
        mock_delay.assert_called_once()
