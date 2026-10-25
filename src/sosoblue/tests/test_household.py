import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import TV, Episode, Item, MediaTypes, Movie, Sources, Status
from sosoblue import household, ratings
from sosoblue.models import ContentRating

SHOW_ID = "1399"
MOVIE_ID = "603"


def fake_tv(media_id):
    """Return show metadata without calling TMDB."""
    return {
        "media_id": media_id,
        "title": "Show",
        "image": "https://example.invalid/show.jpg",
        "max_progress": 6,
        "related": {"seasons": []},
        "details": {"status": "Returning Series"},
    }


def fake_tv_with_seasons(media_id, season_numbers):
    """Return show and season metadata without calling TMDB."""
    data = fake_tv(media_id)
    for number in season_numbers:
        data[f"season/{number}"] = {
            "image": "https://example.invalid/season.jpg",
            "episodes": [
                {"episode_number": episode, "title": f"Episode {episode}"}
                for episode in range(1, 4)
            ],
        }
    return data


def fake_movie(media_id):
    """Return movie metadata without calling TMDB."""
    return {
        "media_id": media_id,
        "title": "Movie",
        "image": "https://example.invalid/movie.jpg",
        "max_progress": 1,
    }


class HouseholdTestCase(TestCase):
    """Three members, the last one limited to 12+, and TMDB mocked."""

    def setUp(self):
        """Create the household."""
        for target, fake in (
            ("app.providers.tmdb.tv", fake_tv),
            ("app.providers.tmdb.tv_with_seasons", fake_tv_with_seasons),
            ("app.providers.tmdb.movie", fake_movie),
        ):
            patcher = patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        user_model = get_user_model()
        self.nicolas = user_model.objects.create_superuser(
            username="nicolas",
            password="12345",  # noqa: S106
        )
        self.audrey = user_model.objects.create_user(
            username="audrey",
            password="12345",  # noqa: S106
        )
        self.meilin = user_model.objects.create_user(
            username="meilin",
            password="12345",  # noqa: S106
        )
        household.set_age_limit(self.meilin, 12)

    def rate(self, media_type, media_id, age, certification=""):
        """Store an age rating."""
        ContentRating.objects.update_or_create(
            media_type=media_type,
            media_id=media_id,
            defaults={"age": age, "certification": certification, "country": "FR"},
        )

    def seen(self, user, season=1):
        """Return the episode numbers a user watched."""
        return sorted(household.watched_episodes(user, SHOW_ID, season))


class AgeTests(TestCase):
    """Parsing ratings and checking limits."""

    def test_parse_age(self):
        """Certifications from several countries give an age."""
        self.assertEqual(ratings.parse_age("FR", "U"), 0)
        self.assertEqual(ratings.parse_age("FR", "-12"), 12)
        self.assertEqual(ratings.parse_age("FR", "16"), 16)
        self.assertEqual(ratings.parse_age("US", "TV-MA"), 18)
        self.assertEqual(ratings.parse_age("US", "PG-13"), 13)
        self.assertEqual(ratings.parse_age("GB", "12A"), 12)
        self.assertIsNone(ratings.parse_age("FR", "NR"))
        self.assertIsNone(ratings.parse_age("US", ""))

    def test_pick_rating_prefers_france(self):
        """The French rating wins, then the US one, then any other."""
        self.assertEqual(
            ratings.pick_rating({"US": ["TV-14"], "FR": ["16"]}),
            ("FR", "16", 16),
        )
        self.assertEqual(
            ratings.pick_rating({"US": ["TV-14"], "FR": [""]}),
            ("US", "TV-14", 14),
        )
        self.assertEqual(ratings.pick_rating({"BR": ["14"]}), ("BR", "14", 14))
        self.assertEqual(ratings.pick_rating({}), ("", "", None))

    def test_is_allowed(self):
        """Limits apply, unrated content only passes when shared."""
        self.assertTrue(household.is_allowed(None, 18, strict=True))
        self.assertTrue(household.is_allowed(12, 12, strict=True))
        self.assertFalse(household.is_allowed(12, 16, strict=False))
        self.assertFalse(household.is_allowed(12, None, strict=True))
        self.assertTrue(household.is_allowed(12, None, strict=False))

    @patch("sosoblue.ratings.services.api_request")
    def test_fetch_and_store(self, mock_request):
        """Ratings are fetched once from TMDB and kept."""
        mock_request.return_value = {
            "results": [
                {"iso_3166_1": "US", "rating": "TV-MA"},
                {"iso_3166_1": "FR", "rating": "16"},
            ],
        }
        rating = ratings.get_rating("tv", SHOW_ID)
        self.assertEqual((rating.age, rating.certification), (16, "16"))
        ratings.get_rating("tv", SHOW_ID)
        mock_request.assert_called_once()

        mock_request.return_value = {
            "results": [
                {
                    "iso_3166_1": "FR",
                    "release_dates": [{"certification": ""}, {"certification": "12"}],
                },
            ],
        }
        self.assertEqual(ratings.get_rating("movie", MOVIE_ID).age, 12)
        self.assertIsNone(ratings.get_rating("season", SHOW_ID))

    @patch("sosoblue.ratings.services.api_request", side_effect=RuntimeError)
    def test_fetch_failure(self, _):
        """A TMDB failure stores nothing."""
        self.assertIsNone(ratings.get_rating("tv", SHOW_ID))
        self.assertFalse(ContentRating.objects.exists())


class MarkingTests(HouseholdTestCase):
    """Marking episodes and movies for another member."""

    def test_mark_and_unmark_episode(self):
        """An episode can be given to a member, once, and taken back."""
        when = timezone.now() - datetime.timedelta(days=1)
        self.assertTrue(household.mark_episode(self.audrey, SHOW_ID, 1, 2, when))
        self.assertFalse(household.mark_episode(self.audrey, SHOW_ID, 1, 2))
        self.assertEqual(self.seen(self.audrey), [(1, 2)])
        episode = Episode.objects.get(related_season__user=self.audrey)
        self.assertEqual(episode.end_date, when)
        self.assertEqual(
            TV.objects.get(user=self.audrey).status,
            Status.IN_PROGRESS.value,
        )

        self.assertTrue(household.unmark_episode(self.audrey, SHOW_ID, 1, 2))
        self.assertFalse(household.unmark_episode(self.audrey, SHOW_ID, 1, 2))
        self.assertEqual(self.seen(self.audrey), [])

    def test_align(self):
        """Aligning copies the missing episodes with their dates."""
        for number in (1, 2, 3):
            household.mark_episode(self.nicolas, SHOW_ID, 1, number)
        household.mark_episode(self.audrey, SHOW_ID, 1, 1)

        self.assertEqual(
            household.align_episodes(self.nicolas, self.audrey, SHOW_ID), 2
        )
        self.assertEqual(self.seen(self.audrey), [(1, 1), (1, 2), (1, 3)])
        self.assertEqual(
            household.align_episodes(self.nicolas, self.audrey, SHOW_ID), 0
        )

    def test_mark_and_unmark_movie(self):
        """A movie can be given to a member and taken back."""
        self.assertTrue(household.mark_movie(self.audrey, MOVIE_ID))
        self.assertFalse(household.mark_movie(self.audrey, MOVIE_ID))
        movie = Movie.objects.get(user=self.audrey)
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertIsNotNone(household.movie_end_date(self.audrey, MOVIE_ID))

        self.assertTrue(household.unmark_movie(self.audrey, MOVIE_ID))
        self.assertFalse(Movie.objects.filter(user=self.audrey).exists())

    def test_mark_planned_movie(self):
        """A planned movie becomes completed instead of duplicated."""
        item = Item.objects.create(
            media_id=MOVIE_ID,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Movie",
        )
        Movie.objects.bulk_create(
            [Movie(user=self.audrey, item=item, status=Status.PLANNING.value)],
        )
        self.assertTrue(household.mark_movie(self.audrey, MOVIE_ID))
        self.assertEqual(
            list(
                Movie.objects.filter(user=self.audrey).values_list("status", flat=True)
            ),
            [Status.COMPLETED.value],
        )


class PanelTests(HouseholdTestCase):
    """The household panel on media pages."""

    def setUp(self):
        """Log in as Nicolas."""
        super().setUp()
        self.client.force_login(self.nicolas)

    def get_panel(self, **params):
        """Return the panel response."""
        return self.client.get(reverse("sosoblue:household_panel"), params)

    def test_season_panel(self):
        """The season panel shows who watched which episode."""
        self.rate("tv", SHOW_ID, 16, "16")
        household.mark_episode(self.nicolas, SHOW_ID, 1, 1)
        household.mark_episode(self.audrey, SHOW_ID, 1, 2)

        response = self.get_panel(
            media_type="season",
            media_id=SHOW_ID,
            season_number=1,
        )
        self.assertContains(response, "sb-household-panel")
        self.assertContains(response, "Episode 1")
        self.assertContains(response, "Episode 2")
        self.assertContains(response, "Limited to 12+")
        rows = response.context["rows"]
        self.assertEqual(
            [row.member.user for row in rows], [self.nicolas, self.audrey, self.meilin]
        )
        self.assertEqual([row.allowed for row in rows], [True, True, False])
        self.assertEqual(rows[1].missing, 1)

    def test_panel_hidden_for_limited_viewer(self):
        """A limited member sees no panel on content above their limit."""
        self.rate("tv", SHOW_ID, 16, "16")
        self.client.force_login(self.meilin)
        response = self.get_panel(media_type="tv", media_id=SHOW_ID)
        self.assertEqual(response.content, b"")

    def test_panel_bad_params(self):
        """Invalid parameters are refused."""
        self.assertEqual(
            self.get_panel(media_type="game", media_id="1").status_code, 400
        )
        self.assertEqual(
            self.get_panel(media_type="season", media_id=SHOW_ID).status_code,
            400,
        )

    def test_toggle_episode(self):
        """Clicking a chip gives the episode, clicking again removes it."""
        self.rate("tv", SHOW_ID, 12, "12")
        household.mark_episode(self.nicolas, SHOW_ID, 1, 3)
        data = {
            "media_type": "season",
            "media_id": SHOW_ID,
            "season_number": 1,
            "episode": 3,
            "member": self.meilin.id,
        }
        url = reverse("sosoblue:household_toggle")

        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertEqual(self.seen(self.meilin), [(1, 3)])
        mine = Episode.objects.get(related_season__user=self.nicolas).end_date
        theirs = Episode.objects.get(related_season__user=self.meilin).end_date
        self.assertEqual(mine, theirs)

        self.client.post(url, data)
        self.assertEqual(self.seen(self.meilin), [])

    def test_toggle_refused_above_age_limit(self):
        """Content above a member's limit cannot be given to them."""
        self.rate("movie", MOVIE_ID, 16, "16")
        response = self.client.post(
            reverse("sosoblue:household_toggle"),
            {"media_type": "movie", "media_id": MOVIE_ID, "member": self.meilin.id},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Movie.objects.filter(user=self.meilin).exists())

    def test_toggle_movie(self):
        """A movie chip gives the movie to the member."""
        self.rate("movie", MOVIE_ID, 0, "U")
        response = self.client.post(
            reverse("sosoblue:household_toggle"),
            {"media_type": "movie", "media_id": MOVIE_ID, "member": self.audrey.id},
        )
        self.assertContains(response, "sb-chip-on")
        self.assertTrue(Movie.objects.filter(user=self.audrey).exists())

    def test_align_view(self):
        """The align button copies the viewer's episodes."""
        self.rate("tv", SHOW_ID, 12, "12")
        household.mark_episode(self.nicolas, SHOW_ID, 1, 1)
        household.mark_episode(self.nicolas, SHOW_ID, 1, 2)
        response = self.client.post(
            reverse("sosoblue:household_align"),
            {"media_type": "tv", "media_id": SHOW_ID, "member": self.audrey.id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen(self.audrey), [(1, 1), (1, 2)])

        response = self.client.post(
            reverse("sosoblue:household_align"),
            {"media_type": "tv", "media_id": SHOW_ID, "member": self.nicolas.id},
        )
        self.assertEqual(response.status_code, 403)


class OverviewTests(HouseholdTestCase):
    """The household page."""

    def setUp(self):
        """Create a show followed by two members."""
        super().setUp()
        for number in (1, 2, 3):
            household.mark_episode(self.nicolas, SHOW_ID, 1, number)
        household.mark_episode(self.audrey, SHOW_ID, 1, 1)

    @patch("sosoblue.views.tasks.fill_content_ratings.delay")
    def test_page(self, mock_fill):
        """Shared shows and recent activity are listed."""
        self.rate("tv", SHOW_ID, 12, "12")
        self.client.force_login(self.nicolas)
        response = self.client.get(reverse("sosoblue:household"))
        self.assertEqual(response.status_code, 200)

        shared = response.context["shared"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].ahead.user, self.nicolas)
        behind = {entry["member"].user: entry["behind"] for entry in shared[0].progress}
        self.assertEqual(behind, {self.nicolas: 0, self.audrey: 2})
        self.assertContains(response, "Household settings")
        mock_fill.assert_not_called()

    @patch("sosoblue.views.tasks.fill_content_ratings.delay")
    def test_limited_viewer(self, mock_fill):
        """A limited member does not see content above their limit or unrated."""
        self.client.force_login(self.meilin)
        response = self.client.get(reverse("sosoblue:household"))
        self.assertEqual(response.context["shared"], [])
        self.assertNotContains(response, "Household settings")
        mock_fill.assert_called_once()

        self.rate("tv", SHOW_ID, 16, "16")
        response = self.client.get(reverse("sosoblue:household"))
        self.assertEqual(response.context["shared"], [])
        activity = {
            member.id: entries for member, entries in response.context["activity"]
        }
        self.assertEqual(activity[self.nicolas.id], [])

        self.rate("tv", SHOW_ID, 10, "10")
        response = self.client.get(reverse("sosoblue:household"))
        self.assertEqual(len(response.context["shared"]), 1)

    def test_settings(self):
        """Only an administrator can change the age limits."""
        url = reverse("sosoblue:household_settings")
        self.client.force_login(self.audrey)
        self.assertEqual(self.client.post(url, {}).status_code, 403)

        self.client.force_login(self.nicolas)
        response = self.client.post(
            url,
            {
                f"age_limit_{self.meilin.id}": "14",
                f"age_limit_{self.audrey.id}": "",
            },
        )
        self.assertRedirects(response, reverse("sosoblue:household"))
        self.assertEqual(household.age_limit_of(self.meilin), 14)
        self.assertIsNone(household.age_limit_of(self.audrey))
        self.assertEqual(
            self.client.post(url, {f"age_limit_{self.meilin.id}": "13"}).status_code,
            400,
        )


class ClearTests(HouseholdTestCase):
    """Emptying an account."""

    def test_clear(self):
        """An administrator empties an account after typing its name."""
        household.mark_episode(self.meilin, SHOW_ID, 1, 1)
        household.mark_movie(self.meilin, MOVIE_ID)
        household.mark_movie(self.audrey, MOVIE_ID)
        url = reverse("sosoblue:household_clear")

        self.client.force_login(self.audrey)
        self.assertEqual(
            self.client.post(
                url, {"member": self.meilin.id, "confirm": "meilin"}
            ).status_code,
            403,
        )

        self.client.force_login(self.nicolas)
        self.client.post(url, {"member": self.meilin.id, "confirm": "wrong"})
        self.assertTrue(TV.objects.filter(user=self.meilin).exists())

        response = self.client.post(
            url, {"member": self.meilin.id, "confirm": "meilin"}
        )
        self.assertRedirects(response, reverse("sosoblue:household"))
        self.assertFalse(TV.objects.filter(user=self.meilin).exists())
        self.assertFalse(
            Episode.objects.filter(related_season__user=self.meilin).exists()
        )
        self.assertFalse(Movie.objects.filter(user=self.meilin).exists())
        self.assertTrue(Movie.objects.filter(user=self.audrey).exists())
