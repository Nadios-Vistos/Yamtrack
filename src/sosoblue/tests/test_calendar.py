import datetime

from django.urls import reverse

from app.models import Item, MediaTypes, Sources
from events.models import Event
from sosoblue import household
from sosoblue.tests.test_household import SHOW_ID, HouseholdTestCase


class CalendarTranslationTests(HouseholdTestCase):
    """The calendar shows month and day names in the user's language."""

    def setUp(self):
        """Track a show with an episode out in October 2026."""
        super().setUp()
        self.nicolas.language = "fr"
        self.nicolas.save()
        self.client.force_login(self.nicolas)
        household.mark_episode(self.nicolas, SHOW_ID, 1, 1)
        season = Item.objects.get(
            media_id=SHOW_ID,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
        )
        Event.objects.create(
            item=season,
            content_number=2,
            datetime=datetime.datetime(2026, 10, 26, 20, 0, tzinfo=datetime.UTC),
        )

    def get(self, view):
        """Return the October 2026 calendar in the given layout."""
        return self.client.get(
            reverse("calendar"),
            {"month": 10, "year": 2026, "view": view},
            HTTP_ACCEPT_LANGUAGE="fr",
        )

    def test_grid(self):
        """Month and weekday headers are translated."""
        response = self.get("grid")
        self.assertContains(response, "Octobre 2026")
        self.assertEqual(response.context["weekday_headers"][0], "Lun")
        self.assertNotContains(response, "October")

    def test_list(self):
        """Each day reads like a French date."""
        response = self.get("list")
        self.assertContains(response, "Lundi 26 octobre")
        self.assertContains(response, "E2 à")
