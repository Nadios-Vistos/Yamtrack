from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from sosoblue import navigation


class NavigationTests(TestCase):
    """The phone tab bar and the media order."""

    def setUp(self):
        """Log in a user."""
        self.user = get_user_model().objects.create_user(
            username="nicolas",
            password="12345",  # noqa: S106
        )
        self.client.force_login(self.user)

    def test_default(self):
        """Without settings, the tab bar keeps its usual tabs."""
        self.assertEqual(
            navigation.chosen_tabs(self.user),
            ["home", "tv", "up_next", "household"],
        )
        response = self.client.get(reverse("sosoblue:navigation"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="sb-tabbar"')

    def test_save(self):
        """Chosen tabs and media order are kept, in order."""
        response = self.client.post(
            reverse("sosoblue:navigation"),
            {"tabs": ["movie", "home", "calendar"], "media": ["movie", "tv"]},
        )
        self.assertRedirects(response, reverse("sosoblue:navigation"))
        user = get_user_model().objects.get(id=self.user.id)
        self.assertEqual(navigation.chosen_tabs(user), ["movie", "home", "calendar"])
        self.assertEqual(navigation.ordered_media_types(user)[:2], ["movie", "tv"])

        response = self.client.get(reverse("home"))
        tabs = [tab["key"] for tab in navigation.tab_bar(user, "/", None)]
        self.assertEqual(tabs, ["movie", "home", "calendar"])
        self.assertContains(response, reverse("calendar"))

    def test_invalid(self):
        """Too few tabs, too many or unknown ones are refused."""
        url = reverse("sosoblue:navigation")
        self.client.post(url, {"tabs": ["home"]})
        self.client.post(
            url, {"tabs": ["home", "tv", "movie", "anime", "game", "book"]}
        )
        self.client.post(url, {"tabs": ["home", "nope"]})
        self.assertEqual(
            navigation.chosen_tabs(get_user_model().objects.get(id=self.user.id)),
            ["home", "tv", "up_next", "household"],
        )

    def test_disabled_media_type(self):
        """A media type turned off in the preferences leaves the bar."""
        navigation.save(self.user, ["home", "anime", "tv"], [])
        user = get_user_model().objects.get(id=self.user.id)
        user.anime_enabled = False
        user.save()
        self.assertEqual(navigation.chosen_tabs(user), ["home", "tv"])
