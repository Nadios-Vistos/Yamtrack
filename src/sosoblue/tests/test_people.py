from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from sosoblue import household, people
from sosoblue.tests.test_household import SHOW_ID, HouseholdTestCase

PERSON = {
    "id": 1253360,
    "name": "Pedro Pascal",
    "profile_path": "/pedro.jpg",
    "biography": "",
    "birthday": "1975-04-02",
    "deathday": None,
    "place_of_birth": "Santiago, Chile",
    "known_for_department": "Acting",
    "combined_credits": {
        "cast": [
            {
                "id": int(SHOW_ID),
                "media_type": "tv",
                "name": "Game of Thrones",
                "poster_path": "/got.jpg",
                "first_air_date": "2011-04-17",
                "character": "Oberyn Martell",
                "episode_count": 7,
                "genre_ids": [18],
            },
            {
                "id": 100088,
                "media_type": "tv",
                "name": "The Last of Us",
                "poster_path": "/tlou.jpg",
                "first_air_date": "2023-01-15",
                "character": "Joel Miller",
                "episode_count": 16,
                "genre_ids": [18],
            },
            {
                "id": 22980,
                "media_type": "tv",
                "name": "Talk Show",
                "first_air_date": "2010-01-01",
                "character": "Self",
                "genre_ids": [10767],
            },
            {
                "id": 1003596,
                "media_type": "movie",
                "title": "Gladiator II",
                "poster_path": "/g2.jpg",
                "release_date": "2024-11-13",
                "character": "Marcus Acacius",
                "genre_ids": [28],
            },
        ],
        "crew": [],
    },
}

SEARCH = {
    "results": [
        {
            "id": 1253360,
            "name": "Pedro Pascal",
            "profile_path": "/pedro.jpg",
            "known_for_department": "Acting",
            "known_for": [{"name": "The Last of Us"}, {"title": "Gladiator II"}],
        },
        {
            "id": 5,
            "name": "Pedro Almodóvar",
            "profile_path": "/pedro2.jpg",
            "known_for_department": "Directing",
            "known_for": [{"title": "Volver"}],
        },
    ],
}


def fake_tmdb(_source, _method, url, params=None):
    """Answer the TMDB calls of the people module."""
    if url.endswith("/search/person"):
        return SEARCH
    if params and params.get("language") == "en-US":
        return {"biography": "Chilean-American actor."}
    return PERSON


class PeopleTestCase(TestCase):
    """TMDB answers with fixed people, and the cache starts empty."""

    def setUp(self):
        """Mock TMDB."""
        super().setUp()
        cache.clear()
        patcher = patch("sosoblue.people.services.api_request", side_effect=fake_tmdb)
        self.api = patcher.start()
        self.addCleanup(patcher.stop)


class NameMatchTests(TestCase):
    """Only people whose name contains the searched words."""

    def test_name_matches(self):
        """Words may be partial and accents do not matter."""
        self.assertTrue(people.name_matches("pedro pas", "Pedro Pascal"))
        self.assertTrue(people.name_matches("almodovar", "Pedro Almodóvar"))
        self.assertFalse(people.name_matches("the office", "Pedro Pascal"))


class PeopleSearchTests(PeopleTestCase):
    """Searching people by name."""

    def test_search(self):
        """Matches keep what they are known for, and are cached."""
        matches = people.search("Pascal")
        self.assertEqual([match["name"] for match in matches], ["Pedro Pascal"])
        self.assertEqual(
            [match["name"] for match in people.search("Pedro")],
            ["Pedro Pascal", "Pedro Almodóvar"],
        )
        self.assertEqual(matches[0]["known_for"], ["The Last of Us", "Gladiator II"])
        people.search("Pascal")
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(people.search("p"), [])


class PersonTests(PeopleTestCase):
    """A person's filmography."""

    def test_person(self):
        """Roles grouped by type, newest first, talk shows left out."""
        with patch.dict(people.tmdb.base_params, {"language": "fr-FR"}):
            person = people.person(1253360)
        self.assertEqual(
            [entry["item"]["title"] for entry in person["shows"]],
            ["The Last of Us", "Game of Thrones"],
        )
        self.assertEqual(person["shows"][0]["roles"], ["Joel Miller"])
        self.assertEqual(person["movies"][0]["item"]["media_type"], "movie")
        self.assertEqual(person["biography"], "Chilean-American actor.")


class PersonPageTests(PeopleTestCase, HouseholdTestCase):
    """The person page and the links to it."""

    def setUp(self):
        """Nicolas follows one of the shows."""
        super().setUp()
        self.client.force_login(self.nicolas)
        household.mark_episode(self.nicolas, SHOW_ID, 1, 1)

    def test_page(self):
        """The page lists the works and which ones are tracked."""
        response = self.client.get(reverse("sosoblue:person", args=[1253360]))
        self.assertContains(response, "Pedro Pascal")
        self.assertContains(response, "Joel Miller")
        self.assertEqual(response.context["tracked_count"], 1)
        tracked = [entry for entry in response.context["shows"] if entry["media"]]
        self.assertEqual(tracked[0]["item"]["media_id"], int(SHOW_ID))

    def test_unknown(self):
        """A person TMDB does not know is a 404."""
        self.api.side_effect = lambda *_args, **_kwargs: {"success": False}
        response = self.client.get(reverse("sosoblue:person", args=[1]))
        self.assertEqual(response.status_code, 404)

    def test_search_strip(self):
        """A TV search also shows the matching people."""
        empty = {"page": 1, "total_pages": 0, "total_results": 0, "results": []}
        with patch("app.views.services.search", return_value=empty):
            response = self.client.get(
                reverse("search"),
                {"media_type": "tv", "q": "Pedro Pascal"},
            )
        self.assertContains(response, reverse("sosoblue:person", args=[1253360]))
        self.assertNotContains(response, "Almodóvar")
