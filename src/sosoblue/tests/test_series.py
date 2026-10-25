import datetime
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status
from events.models import Event, SentinelDatetime
from sosoblue import series
from sosoblue.models import SeriesState, SeriesStatus, state_from_tmdb
from sosoblue.signals import refresh_after_calendar_reload


def tv_metadata(status, next_season=None):
    """Return the part of the TMDB metadata the refresh reads."""
    return {
        "details": {"status": status, "last_air_date": "2025-05-01"},
        "next_episode_season": next_season,
    }


class StateTests(TestCase):
    """TMDB status mapping."""

    def test_mapping(self):
        """Each TMDB status maps to a simplified state."""
        self.assertEqual(state_from_tmdb("Returning Series"), SeriesState.ONGOING)
        self.assertEqual(state_from_tmdb("In Production"), SeriesState.IN_PRODUCTION)
        self.assertEqual(state_from_tmdb("Ended"), SeriesState.ENDED)
        self.assertEqual(state_from_tmdb("Canceled"), SeriesState.CANCELED)
        self.assertIsNone(state_from_tmdb(""))
        self.assertIsNone(state_from_tmdb(None))


class SeriesTestCase(TestCase):
    """Common fixtures."""

    def setUp(self):
        """Create a user and helpers."""
        self.credentials = {"username": "nicolas", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.now = timezone.now()

    def make_show(self, media_id, status, title=None):
        """Create a tracked TMDB show."""
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title or f"Show {media_id}",
        )
        # bulk_create skips TV.save(), which fetches metadata on completion
        TV.objects.bulk_create([TV(item=item, user=self.user, status=status)])
        return item, TV.objects.get(item=item, user=self.user)

    def season_item(self, media_id, number):
        """Return the season item of a show."""
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=number,
            defaults={"title": f"Show {media_id}"},
        )
        return item

    def add_event(self, media_id, season, episode, when):
        """Create a release event for an episode."""
        return Event.objects.create(
            item=self.season_item(media_id, season),
            content_number=episode,
            datetime=when,
        )

    def watch(self, tv, season, episodes):
        """Mark episodes of a season as watched."""
        season_obj = Season.objects.filter(related_tv=tv, item__season_number=season)
        if season_obj.exists():
            season_obj = season_obj.get()
        else:
            season_obj = Season(
                item=self.season_item(tv.item.media_id, season),
                related_tv=tv,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )
            Season.objects.bulk_create([season_obj])
            season_obj = Season.objects.get(related_tv=tv, item__season_number=season)
        episode_objs = []
        for number in episodes:
            item, _ = Item.objects.get_or_create(
                media_id=tv.item.media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=season,
                episode_number=number,
                defaults={"title": tv.item.title},
            )
            episode_objs.append(
                Episode(item=item, related_season=season_obj, end_date=self.now),
            )
        Episode.objects.bulk_create(episode_objs)

    def set_status(self, item, tmdb_status):
        """Store a TMDB status for a show."""
        SeriesStatus.objects.update_or_create(
            item=item,
            defaults={"tmdb_status": tmdb_status},
        )


class RefreshTests(SeriesTestCase):
    """Storing the TMDB status."""

    @patch("sosoblue.series.tmdb.tv")
    def test_refresh_all_tracked(self, mock_tv):
        """Every tracked show gets a status."""
        mock_tv.return_value = tv_metadata("Returning Series", 3)
        item, _ = self.make_show("1", Status.COMPLETED.value)
        Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Not tracked",
        )

        self.assertEqual(series.refresh_series_status(), 1)
        status = SeriesStatus.objects.get(item=item)
        self.assertEqual(status.state, SeriesState.ONGOING)
        self.assertEqual(status.next_episode_season, 3)
        self.assertEqual(status.last_air_date, datetime.date(2025, 5, 1))
        self.assertEqual(series.missing_status_count(), 0)

    @patch("sosoblue.series.tmdb.tv")
    def test_refresh_given_items_skips_other_types(self, mock_tv):
        """Only TMDB TV items are refreshed."""
        mock_tv.return_value = tv_metadata("Ended")
        item, _ = self.make_show("1", Status.COMPLETED.value)
        movie = Item.objects.create(
            media_id="9",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
        )

        self.assertEqual(series.refresh_series_status([item, movie]), 1)
        mock_tv.assert_called_once_with("1")

    @patch("sosoblue.series.tmdb.tv", side_effect=RuntimeError)
    def test_refresh_survives_errors(self, _):
        """A failing show does not stop the refresh."""
        self.make_show("1", Status.COMPLETED.value)
        self.assertEqual(series.refresh_series_status(), 0)

    @patch("sosoblue.signals.ratings.fill_missing")
    @patch("sosoblue.series.refresh_series_status")
    def test_signal_after_calendar_reload(self, mock_refresh, mock_ratings):
        """The calendar reload triggers a refresh of the same items."""
        sender = SimpleNamespace(name="Reload calendar")
        refresh_after_calendar_reload(
            sender=sender,
            kwargs={"items_to_process": ["x"]},
            state="SUCCESS",
        )
        mock_refresh.assert_called_once_with(["x"])
        mock_ratings.assert_called_once_with(["x"])

        mock_refresh.reset_mock()
        refresh_after_calendar_reload(
            sender=SimpleNamespace(name="Other"),
            kwargs={},
            state="SUCCESS",
        )
        refresh_after_calendar_reload(sender=sender, kwargs={}, state="FAILURE")
        mock_refresh.assert_not_called()


class WaitingShowsTests(SeriesTestCase):
    """Splitting the shows waiting for more."""

    def test_completed_with_dated_episode(self):
        """A completed show with an announced episode is dated."""
        item, _ = self.make_show("1", Status.COMPLETED.value)
        self.set_status(item, "Returning Series")
        self.add_event("1", 1, 8, self.now - datetime.timedelta(days=30))
        self.add_event("1", 2, 1, self.now + datetime.timedelta(days=10))
        self.add_event("1", 2, 2, self.now + datetime.timedelta(days=17))

        shows = series.waiting_shows(self.user)
        self.assertEqual([s.item for s in shows["dated"]], [item])
        self.assertEqual(shows["dated"][0].next_season, 2)
        self.assertEqual(shows["dated"][0].next_episode, 1)

    def test_dated_sorted_by_date(self):
        """The soonest episode comes first."""
        later, _ = self.make_show("1", Status.COMPLETED.value)
        sooner, _ = self.make_show("2", Status.COMPLETED.value)
        self.add_event("1", 2, 1, self.now + datetime.timedelta(days=40))
        self.add_event("2", 4, 1, self.now + datetime.timedelta(days=5))

        shows = series.waiting_shows(self.user)
        self.assertEqual([s.item for s in shows["dated"]], [sooner, later])

    def test_renewed_without_date(self):
        """A returning show with no date is undated; ended shows are hidden."""
        renewed, _ = self.make_show("1", Status.COMPLETED.value)
        self.set_status(renewed, "Returning Series")
        ended, _ = self.make_show("2", Status.COMPLETED.value)
        self.set_status(ended, "Ended")
        canceled, _ = self.make_show("3", Status.COMPLETED.value)
        self.set_status(canceled, "Canceled")
        unknown, _ = self.make_show("4", Status.COMPLETED.value)

        shows = series.waiting_shows(self.user)
        self.assertEqual([s.item for s in shows["undated"]], [renewed])
        self.assertEqual([s.item for s in shows["unknown"]], [unknown])
        self.assertEqual(shows["dated"], [])

    def test_sentinel_date_is_undated(self):
        """An announced season without a date goes to undated."""
        item, _ = self.make_show("1", Status.COMPLETED.value)
        self.add_event("1", 3, None, SentinelDatetime.max_datetime())

        shows = series.waiting_shows(self.user)
        self.assertEqual([s.item for s in shows["undated"]], [item])
        self.assertEqual(shows["undated"][0].next_season, 3)

    def test_in_progress_up_to_date(self):
        """An in-progress show with every aired episode watched is waiting."""
        item, tv = self.make_show("1", Status.IN_PROGRESS.value)
        self.add_event("1", 1, 1, self.now - datetime.timedelta(days=14))
        self.add_event("1", 1, 2, self.now - datetime.timedelta(days=7))
        self.add_event("1", 1, 3, self.now + datetime.timedelta(days=1))
        self.watch(tv, 1, [1, 2])

        shows = series.waiting_shows(self.user)
        self.assertEqual([s.item for s in shows["dated"]], [item])

    def test_in_progress_catching_up(self):
        """An in-progress show with unwatched aired episodes is not waiting."""
        _, tv = self.make_show("1", Status.IN_PROGRESS.value)
        self.add_event("1", 1, 1, self.now - datetime.timedelta(days=14))
        self.add_event("1", 1, 2, self.now - datetime.timedelta(days=7))
        self.add_event("1", 1, 3, self.now + datetime.timedelta(days=1))
        self.watch(tv, 1, [1])

        shows = series.waiting_shows(self.user)
        self.assertEqual(shows["dated"], [])
        self.assertEqual(shows["undated"], [])

    def test_in_progress_without_announcement(self):
        """An in-progress show with nothing announced is not waiting."""
        item, _ = self.make_show("1", Status.IN_PROGRESS.value)
        self.set_status(item, "Returning Series")

        shows = series.waiting_shows(self.user)
        self.assertEqual(shows, {"dated": [], "undated": [], "unknown": []})

    def test_other_users_and_statuses_ignored(self):
        """Paused shows and other users' shows are left out."""
        self.make_show("1", Status.PAUSED.value)
        credentials = {"username": "audrey", "password": "12345"}
        other = get_user_model().objects.create_user(**credentials)
        item = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Hers",
        )
        TV.objects.bulk_create(
            [TV(item=item, user=other, status=Status.COMPLETED.value)],
        )
        self.set_status(item, "Returning Series")

        shows = series.waiting_shows(self.user)
        self.assertEqual(shows, {"dated": [], "undated": [], "unknown": []})


class ViewTests(SeriesTestCase):
    """The waiting page."""

    def setUp(self):
        """Log in."""
        super().setUp()
        self.client.force_login(self.user)

    def test_waiting_page(self):
        """The page lists the waiting shows."""
        item, _ = self.make_show("1", Status.COMPLETED.value, title="Fallout")
        self.set_status(item, "Returning Series")

        response = self.client.get(reverse("sosoblue:waiting"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fallout")
        self.assertContains(response, "sb-ongoing")
        self.assertContains(response, reverse("sosoblue:waiting"))

    def test_waiting_page_in_french(self):
        """The page is translated."""
        self.user.language = "fr"
        self.user.save(update_fields=["language"])
        response = self.client.get(reverse("sosoblue:waiting"))
        self.assertContains(response, "À suivre")

    @patch("sosoblue.views.tasks.refresh_series_status.delay")
    def test_refresh(self, mock_delay):
        """The refresh button starts the task."""
        response = self.client.post(reverse("sosoblue:refresh_status"))
        self.assertRedirects(response, reverse("sosoblue:waiting"))
        mock_delay.assert_called_once()


class TemplateTagTests(SeriesTestCase):
    """The card badge."""

    def render(self, item, request):
        """Render the tag for an item."""
        template = Template(
            "{% load sosoblue_tags %}{% series_state item as s %}[{{ s }}]",
        )
        return template.render(Context({"item": item, "request": request}))

    def test_series_state_tag(self):
        """The tag returns the state and loads statuses once per request."""
        item, _ = self.make_show("1", Status.COMPLETED.value)
        self.set_status(item, "Canceled")
        movie = Item(media_id="1", source=Sources.TMDB.value, media_type="movie")
        request = RequestFactory().get("/")

        with self.assertNumQueries(1):
            self.assertEqual(self.render(item, request), "[canceled]")
            self.assertEqual(self.render(item, request), "[canceled]")
            self.assertEqual(self.render(movie, request), "[None]")
        search_result = {"media_id": 1, "source": "tmdb", "media_type": "tv"}
        self.assertEqual(self.render(search_result, request), "[canceled]")
