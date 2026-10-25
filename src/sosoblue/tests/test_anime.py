from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from app.models import TV, Anime, Episode, Status, UserMessage
from sosoblue import anime, household, tasks
from sosoblue.history import runner
from sosoblue.models import (
    HistoryImport,
    HistoryImportStatus,
    ImportGroup,
    ImportLine,
)
from sosoblue.tests.test_household import SHOW_ID, HouseholdTestCase, fake_tv

TVDB_ID = 81189
MAL_ID = "5114"
# Season 1 of the TVDB show is MyAnimeList entry 5114, episodes as is.
MAPPING = {f"tvdb_show:{TVDB_ID}:s1": {f"mal:{MAL_ID}": {}}}


def fake_tv_with_tvdb(media_id):
    """Return show metadata with a TVDB id."""
    return {**fake_tv(media_id), "tvdb_id": TVDB_ID}


def fake_anime(media_id):
    """Return MyAnimeList metadata without calling the API."""
    return {
        "media_id": media_id,
        "title": "Anime",
        "image": "https://example.invalid/anime.jpg",
        "max_progress": 3,
    }


class AnimeTestCase(HouseholdTestCase):
    """TMDB and MyAnimeList mocked, with a TVDB id on the show."""

    def setUp(self):
        """Add the MyAnimeList and TVDB mocks."""
        super().setUp()
        for target, fake in (
            ("app.providers.mal.anime", fake_anime),
            ("app.providers.tmdb.tv", fake_tv_with_tvdb),
        ):
            patcher = patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)


class MappingTests(AnimeTestCase):
    """Matching TVDB episodes with MyAnimeList."""

    def test_to_mal(self):
        """Mapped seasons give the MyAnimeList entry, others nothing."""
        self.assertEqual(anime.to_mal(MAPPING, TVDB_ID, 1, 2), (MAL_ID, 2))
        self.assertIsNone(anime.to_mal(MAPPING, TVDB_ID, 2, 1))
        self.assertIsNone(anime.to_mal(MAPPING, TVDB_ID, 0, 1))
        self.assertIsNone(anime.to_mal(None, TVDB_ID, 1, 1))
        self.assertEqual(anime.tvdb_id(SHOW_ID), TVDB_ID)

    def test_mark_anime(self):
        """Progress only moves forward, and completes at the last episode."""
        early = timezone.now() - timezone.timedelta(days=3)
        now = timezone.now()
        self.assertTrue(anime.mark_anime(self.audrey, MAL_ID, 1, early))
        entry = Anime.objects.get(user=self.audrey)
        self.assertEqual((entry.progress, entry.status), (1, Status.IN_PROGRESS.value))
        self.assertFalse(anime.mark_anime(self.audrey, MAL_ID, 1, now))
        self.assertTrue(anime.mark_anime(self.audrey, MAL_ID, 3, now))
        entry.refresh_from_db()
        self.assertEqual((entry.progress, entry.status), (3, Status.COMPLETED.value))
        self.assertEqual(entry.end_date, now)


class ConvertTests(AnimeTestCase):
    """Moving imported anime out of the series."""

    def test_convert(self):
        """A show fully mapped moves; one with unmapped episodes stays."""
        household.mark_episode(self.nicolas, SHOW_ID, 1, 1)
        household.mark_episode(self.nicolas, SHOW_ID, 1, 2)
        household.mark_episode(self.audrey, SHOW_ID, 1, 1)
        household.mark_episode(self.audrey, SHOW_ID, 2, 1)

        moved = anime.convert_user_shows(self.nicolas, MAPPING)
        self.assertEqual(moved, ["Show"])
        self.assertFalse(TV.objects.filter(user=self.nicolas).exists())
        self.assertFalse(
            Episode.objects.filter(related_season__user=self.nicolas).exists()
        )
        self.assertEqual(Anime.objects.get(user=self.nicolas).progress, 2)

        self.assertEqual(anime.convert_user_shows(self.audrey, MAPPING), [])
        self.assertTrue(TV.objects.filter(user=self.audrey).exists())

    @patch("sosoblue.anime.anime_mappings.fetch_mapping_data", return_value=MAPPING)
    def test_task_and_view(self, _):
        """The button starts the task, which reports to the administrator."""
        household.mark_episode(self.meilin, SHOW_ID, 1, 1)
        url = reverse("sosoblue:household_convert_anime")
        self.client.force_login(self.audrey)
        self.assertEqual(self.client.post(url).status_code, 403)

        self.client.force_login(self.nicolas)
        with patch("sosoblue.views.tasks.convert_anime.delay") as delay:
            self.assertRedirects(self.client.post(url), reverse("sosoblue:household"))
            delay.assert_called_once_with(self.nicolas.id)

        tasks.convert_anime(self.nicolas.id)
        message = UserMessage.objects.get(user=self.nicolas)
        self.assertIn("meilin: Show", message.message)
        self.assertTrue(Anime.objects.filter(user=self.meilin).exists())


@patch("sosoblue.anime.anime_mappings.fetch_mapping_data", return_value=MAPPING)
class ImportAnimeTests(AnimeTestCase):
    """History imports put anime in the Anime section."""

    def make_import(self, keys):
        """Return an import of one show with the given episodes."""
        history_import = HistoryImport.objects.create(
            user=self.nicolas,
            created_by=self.nicolas,
            source="netflix",
            status=HistoryImportStatus.REVIEW,
        )
        group = ImportGroup.objects.create(
            history_import=history_import,
            kind="tv",
            name="Show",
            media_type="tv",
            media_id=SHOW_ID,
        )
        when = timezone.now()
        for season, episode in keys:
            ImportLine.objects.create(
                group=group,
                raw="x",
                watched_at=when,
                matched_season=season,
                matched_episode=episode,
                state=ImportLine.MATCHED,
            )
        return history_import, group

    def test_import_and_undo(self, _):
        """A fully mapped show becomes anime; undo removes it."""
        history_import, group = self.make_import(((1, 1), (1, 2)))
        runner.apply_import(history_import)
        self.assertEqual(Anime.objects.get(user=self.nicolas).progress, 2)
        self.assertFalse(TV.objects.filter(user=self.nicolas).exists())
        self.assertEqual(
            sorted(group.lines.values_list("mal_id", "state")),
            [(MAL_ID, "imported"), (MAL_ID, "imported")],
        )

        runner.undo_import(history_import)
        self.assertFalse(Anime.objects.filter(user=self.nicolas).exists())

    def test_partly_mapped_stays_tv(self, _):
        """A show with an unmapped episode is imported as TV entirely."""
        history_import, _group = self.make_import(((1, 1), (2, 1)))
        runner.apply_import(history_import)
        self.assertFalse(Anime.objects.exists())
        self.assertEqual(self.seen(self.nicolas, None), [(1, 1), (2, 1)])

        runner.undo_import(history_import)
        self.assertFalse(TV.objects.filter(user=self.nicolas).exists())
