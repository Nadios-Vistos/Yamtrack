import datetime
from unittest.mock import patch

from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import Item, MediaTypes, Sources
from events.models import Event
from sosoblue import home, household
from sosoblue.tests.test_household import SHOW_ID, HouseholdTestCase


class NextEpisodeTests(TestCase):
    """Choosing the next episode to watch."""

    def test_next_episode(self):
        """The episode after the last watched, else the first missed."""
        aired = {(1, 1), (1, 2), (1, 3), (2, 1)}
        self.assertEqual(home.next_episode({(1, 1)}, aired), (1, 2))
        self.assertEqual(home.next_episode({(1, 1), (1, 3)}, aired), (2, 1))
        self.assertEqual(home.next_episode({(2, 1)}, aired), (1, 1))
        self.assertEqual(home.next_episode(set(), aired), (1, 1))
        self.assertIsNone(home.next_episode(aired, aired))


class HomeBlocksTests(HouseholdTestCase):
    """The resume and this-week blocks."""

    def setUp(self):
        """Watch an episode and add release events."""
        super().setUp()
        self.client.force_login(self.nicolas)
        household.mark_episode(self.nicolas, SHOW_ID, 1, 1)
        season = Item.objects.get(
            media_id=SHOW_ID,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
        )
        now = timezone.now()
        for number, when in (
            (1, now - datetime.timedelta(days=14)),
            (2, now - datetime.timedelta(days=7)),
            (3, now + datetime.timedelta(days=2)),
        ):
            Event.objects.create(item=season, content_number=number, datetime=when)

    def test_resume_and_week(self):
        """The show to resume and the coming episode are listed."""
        response = self.client.get(reverse("sosoblue:home_top"))
        resume = response.context["resume"]
        self.assertEqual(len(resume), 1)
        self.assertEqual((resume[0].season, resume[0].episode), (1, 2))
        self.assertEqual(resume[0].episode_title, "Episode 2")
        self.assertEqual(resume[0].percent, 50)
        week = response.context["week"]
        self.assertEqual(len(week), 1)
        self.assertEqual(week[0][1][0].content_number, 3)
        self.assertContains(response, "sb-resume-check")

    def test_mark_next(self):
        """The check button marks the episode and refreshes the blocks."""
        url = reverse("sosoblue:home_mark")
        response = self.client.post(
            url,
            {"media_id": SHOW_ID, "season": 1, "episode": 2},
        )
        self.assertEqual(response.context["resume"], [])
        self.assertEqual(self.seen(self.nicolas), [(1, 1), (1, 2)])

        response = self.client.post(
            url,
            {"media_id": SHOW_ID, "season": 1, "episode": 3, "refresh": "1"},
        )
        self.assertEqual(response["HX-Refresh"], "true")
        self.assertEqual(
            self.client.post(url, {"media_id": "x"}).status_code,
            400,
        )

    @patch("sosoblue.artwork.backdrop", return_value="https://example.invalid/b.jpg")
    def test_detail_hero(self, _):
        """The media page header offers the next episode."""
        request = RequestFactory().get("/")
        request.user = self.nicolas
        template = Template(
            "{% load sosoblue_tags %}{% detail_hero media as hero %}"
            "{{ hero.backdrop }}|{{ hero.year }}|{{ hero.next.0 }}-{{ hero.next.1 }}",
        )
        media = {
            "source": "tmdb",
            "media_type": "tv",
            "media_id": SHOW_ID,
            "details": {"first_air_date": "2011-04-17"},
        }
        rendered = template.render(Context({"media": media, "request": request}))
        self.assertEqual(rendered, "https://example.invalid/b.jpg|2011|1-2")
        media["source"] = "manual"
        self.assertEqual(
            template.render(Context({"media": media, "request": request})),
            "||-",
        )


class InlineBlocksTests(HomeBlocksTests):
    """The blocks render with the page, so nothing shifts once it loads."""

    def test_home_page_has_blocks(self):
        """The home page contains the resume block without a second request."""
        response = self.client.get(reverse("home"))
        self.assertContains(response, "sb-resume-check")
        self.assertNotContains(response, "sb-skeleton")

    def test_panel_tag(self):
        """The household panel tag returns the panel of a show."""
        request = RequestFactory().get("/")
        request.user = self.nicolas
        template = Template(
            "{% load sosoblue_tags %}{% household_panel_context media as panel %}"
            "{{ panel.media_type }}:{{ panel.rows|length }}",
        )
        media = {"source": "tmdb", "media_type": "tv", "media_id": SHOW_ID}
        self.assertEqual(
            template.render(Context({"media": media, "request": request})),
            "tv:3",
        )
