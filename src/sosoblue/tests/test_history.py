import datetime
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from sosoblue import household
from sosoblue.history import parsers, runner
from sosoblue.history.matching import (
    TmdbClient,
    find_movie,
    find_show,
    match_episode,
    norm,
)
from sosoblue.history.plex import PlexClient, PlexError, plex_entries
from sosoblue.models import (
    HistoryImport,
    HistoryImportStatus,
    ImportGroup,
    ImportLine,
)

NETFLIX_CSV = """Title,Date
"Frankenstein","10/24/26"
"Ghosts : Fantômes à la maison: Saison 1: Le pilote","10/20/26"
"Ghosts : Fantômes à la maison: Saison 1: La fête","10/21/26"
"Ghosts : Fantômes à la maison: Saison 2: Retour","10/22/26"
"Ghosts : Fantômes à la maison: Saison 2: Chapitre inconnu","10/23/26"
"Introuvable: Épisode A","10/01/26"
"Introuvable: Épisode B","10/02/26"
"Titre mystère","09/30/26"
"Cursed : La rebelle: Saison 1: Seule","09/29/26"
"Daybreak: Saison 1 (Bande-annonce)","09/28/26"
"""

PRIME_CSV = (
    '"Date Watched","Type","Title","Episode Title"\n'
    '"2026-09-03 19:58:29.723","Series","Ghosts - Saison 2","Épisode 2: Fin"\n'
    '"2026-09-01 20:00:00.000","Movie","Frankenstein",""\n'
    '"2026-09-01 19:00:00.000","Movie","Épisode 0: Bande-annonce officielle",""\n'
)

SHOWS = {
    "100": {
        "id": 100,
        "name": "Ghosts : Fantômes à la maison",
        "original_name": "Ghosts",
        "poster_path": "/ghosts.jpg",
        "seasons": [
            {"season_number": 1, "episode_count": 3},
            {"season_number": 2, "episode_count": 2},
        ],
    },
    "400": {
        "id": 400,
        "name": "Gotham",
        "original_name": "Gotham",
        "seasons": [{"season_number": 1, "episode_count": 2}],
    },
    "200": {
        "id": 200,
        "name": "Ghosts",
        "original_name": "Ghosts",
        "seasons": [{"season_number": 1, "episode_count": 2}],
    },
}
EPISODES = {
    ("100", 1, "fr-FR"): ["Le pilote", "La fête", "Le départ"],
    ("100", 2, "fr-FR"): ["Retour", "Fin"],
    ("100", 1, "en-US"): ["Pilot", "The Party", "Leaving"],
    ("100", 2, "en-US"): ["Return", "The End"],
    ("200", 1, "fr-FR"): ["Alison", "Le manoir"],
    ("400", 1, "fr-FR"): ["Pilote", "Bruce Wayne"],
}
MOVIES = {
    "300": {"id": 300, "title": "Frankenstein", "release_date": "2025-10-17"},
    "301": {"id": 301, "title": "Frankenstein", "release_date": "1931-11-21"},
}
SEARCHES = {
    ("tv", "Ghosts : Fantômes à la maison"): [SHOWS["100"]],
    ("tv", "Ghosts"): [SHOWS["200"], SHOWS["100"]],
    ("tv", "Gotham"): [SHOWS["400"]],
    ("movie", "Frankenstein"): [MOVIES["301"], MOVIES["300"]],
}


class FakeTmdb(TmdbClient):
    """TMDB client answering from the data above."""

    def __init__(self):
        """Use French then English, like the default settings."""
        super().__init__()
        self.languages = ["fr-FR", "en-US"]

    def get(self, path, **params):
        """Return the canned response of a path."""
        parts = path.split("/")
        if parts[0] == "search":
            return {"results": SEARCHES.get((parts[1], params["query"]), [])}
        if parts[0] == "tv" and len(parts) == 2:  # noqa: PLR2004
            return SHOWS.get(parts[1])
        if parts[0] == "tv":
            names = EPISODES.get((parts[1], int(parts[3]), params["language"]), [])
            return {
                "episodes": [
                    {"episode_number": number, "name": name}
                    for number, name in enumerate(names, start=1)
                ],
            }
        if parts[0] == "movie":
            return MOVIES.get(parts[1])
        return None


def aware(*args):
    """Return an aware datetime."""
    return timezone.make_aware(datetime.datetime(*args))  # noqa: DTZ001


class ParserTests(TestCase):
    """Reading the exports."""

    def test_netflix(self):
        """Episodes, shows without seasons and other titles are told apart."""
        entries = parsers.parse_netflix(NETFLIX_CSV)
        kinds = [(entry.kind, entry.name, entry.season) for entry in entries]
        self.assertIn(("tv", "Ghosts : Fantômes à la maison", 1), kinds)
        self.assertIn(("tv", "Introuvable", None), kinds)
        self.assertIn(("unknown", "Frankenstein", None), kinds)
        self.assertIn(("unknown", "Titre mystère", None), kinds)
        self.assertIn(("tv", "Cursed : La rebelle", 1), kinds)
        self.assertFalse([entry for entry in entries if "Daybreak" in entry.raw])
        self.assertEqual(entries[0].watched_at.date(), datetime.date(2026, 10, 20))

    def test_netflix_day_first_dates(self):
        """Exports with day-first dates are detected."""
        entries = parsers.parse_netflix(
            'Title,Date\n"A","24/10/2026"\n"B","03/10/2026"\n'
        )
        self.assertEqual(
            [entry.watched_at.date() for entry in entries],
            [datetime.date(2026, 10, 24), datetime.date(2026, 10, 3)],
        )

    def test_season_number(self):
        """Season labels in words or digits give a number."""
        self.assertEqual(parsers.season_number("Saison 3"), 3)
        self.assertEqual(parsers.season_number("Partie deux"), 2)
        self.assertEqual(parsers.season_number("Mini-série"), 1)
        self.assertIsNone(parsers.season_number("Collection"))

    def test_prime(self):
        """Prime lines give the season and episode numbers."""
        show, movie = parsers.parse_prime(PRIME_CSV)
        self.assertEqual(
            (
                show.kind,
                show.name,
                show.season,
                show.episode_number,
                show.episode_title,
            ),
            ("tv", "Ghosts", 2, 2, "Fin"),
        )
        self.assertEqual((movie.kind, movie.name), ("movie", "Frankenstein"))

    def test_wrong_format(self):
        """A file with other columns is refused."""
        with self.assertRaises(parsers.HistoryFormatError):
            parsers.parse_netflix(PRIME_CSV)
        with self.assertRaises(parsers.HistoryFormatError):
            parsers.parse_prime(NETFLIX_CSV)


class MatchingTests(TestCase):
    """Finding shows, movies and episodes."""

    def setUp(self):
        """Use the fake TMDB."""
        self.client = FakeTmdb()

    def test_norm(self):
        """Accents, case and punctuation are ignored."""
        self.assertEqual(norm("L'Œuvre — Été !"), norm("loeuvre ete"))

    def test_find_show_uses_episode_titles(self):
        """Between two shows with the same name, the episodes decide."""
        self.assertEqual(find_show(self.client, "Ghosts")["id"], 200)
        self.assertEqual(find_show(self.client, "Ghosts", ["Retour", "Fin"])["id"], 100)
        self.assertIsNone(find_show(self.client, "Nothing"))

    def test_find_movie(self):
        """The year, or the date watched, picks between remakes."""
        self.assertEqual(
            find_movie(self.client, "Frankenstein", "2026-10-24")["id"], 300
        )
        self.assertEqual(
            find_movie(self.client, "Frankenstein", "1990-01-01")["id"], 301
        )
        self.assertEqual(find_movie(self.client, "Frankenstein", "", 1931)["id"], 301)

    def test_match_episode(self):
        """Exact, English, truncated, fuzzy and generic titles are found."""
        seasons = [1, 2, 0]
        self.assertEqual(
            match_episode(self.client, "100", seasons, "La fête", 1), (1, 2)
        )
        self.assertEqual(match_episode(self.client, "100", seasons, "The End"), (2, 2))
        self.assertEqual(
            match_episode(self.client, "100", seasons, "Le dép..."), (1, 3)
        )
        self.assertEqual(
            match_episode(self.client, "100", seasons, "Le pilotes", 1), (1, 1)
        )
        self.assertEqual(
            match_episode(self.client, "100", seasons, "Épisode 2", 2), (2, 2)
        )
        self.assertIsNone(match_episode(self.client, "100", seasons, "Autre chose", 1))
        self.assertEqual(
            match_episode(self.client, "100", seasons, "Fantômes: Retour", 2),
            (2, 1),
        )


@patch("sosoblue.history.runner.ratings.get_rating", return_value=None)
@patch("sosoblue.history.runner.TmdbClient", FakeTmdb)
class RunnerTests(TestCase):
    """Matching and importing a whole history."""

    def setUp(self):
        """Create the user and a Netflix import."""
        patcher = patch("sosoblue.history.runner.anime.load_mapping", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = get_user_model().objects.create_user(
            username="nicolas",
            password="12345",  # noqa: S106
        )
        self.history_import = HistoryImport.objects.create(
            user=self.user,
            created_by=self.user,
            source="netflix",
            raw_data=NETFLIX_CSV,
        )

    def group(self, name):
        """Return the group of a title."""
        return ImportGroup.objects.get(history_import=self.history_import, name=name)

    def test_match(self, _):
        """Titles are grouped and matched, unknown ones left for review."""
        runner.match_import(self.history_import)
        self.history_import.refresh_from_db()
        self.assertEqual(self.history_import.status, HistoryImportStatus.REVIEW)
        self.assertEqual(self.history_import.raw_data, "")
        self.assertEqual(self.history_import.progress, self.history_import.total)

        ghosts = self.group("Ghosts : Fantômes à la maison")
        self.assertEqual((ghosts.media_type, ghosts.media_id), ("tv", "100"))
        self.assertTrue(ghosts.selected)
        matched = sorted(
            ghosts.lines.filter(state=ImportLine.MATCHED).values_list(
                "matched_season",
                "matched_episode",
            ),
        )
        self.assertEqual(matched, [(1, 1), (1, 2), (2, 1), (2, 2)])
        # Season 2 had as many titles as episodes: the unknown one is covered
        self.assertTrue(ghosts.lines.filter(state=ImportLine.COVERED).exists())

        movie = self.group("Frankenstein")
        self.assertEqual((movie.media_type, movie.media_id), ("movie", "300"))

        missing = self.group("Introuvable")
        self.assertEqual(missing.media_id, "")
        self.assertFalse(missing.selected)

    def test_show_watched_once(self, _):
        """A single "Show: Episode" title is matched as an episode of the show."""
        self.history_import.raw_data = 'Title,Date\n"Gotham: Bruce Wayne","10/01/26"\n'
        self.history_import.save()
        runner.match_import(self.history_import)
        group = self.group("Gotham: Bruce Wayne")
        self.assertEqual((group.media_type, group.media_id), ("tv", "400"))
        line = group.lines.get()
        self.assertEqual((line.matched_season, line.matched_episode), (1, 2))

    def test_age_limit(self, mock_rating):
        """Titles above the account's age limit are left out by default."""
        household.set_age_limit(self.user, 12)
        mock_rating.return_value = MagicMock(age=16)
        runner.match_import(self.history_import)
        ghosts = self.group("Ghosts : Fantômes à la maison")
        self.assertTrue(ghosts.above_limit)
        self.assertFalse(ghosts.selected)
        self.assertEqual(ghosts.age, 16)

    def test_failure(self, _):
        """An unreadable file fails the import with the reason."""
        self.history_import.raw_data = "nope"
        self.history_import.save()
        runner.match_import(self.history_import)
        self.history_import.refresh_from_db()
        self.assertEqual(self.history_import.status, HistoryImportStatus.FAILED)

    @patch("sosoblue.history.runner.household.mark_movie", return_value=False)
    @patch(
        "sosoblue.history.runner.household.mark_episode",
        side_effect=[True] * 3 + [False],
    )
    def test_apply(self, mock_episode, mock_movie, _):
        """Selected groups are imported, existing views reported."""
        runner.match_import(self.history_import)
        runner.apply_import(self.history_import)
        self.history_import.refresh_from_db()
        self.assertEqual(self.history_import.status, HistoryImportStatus.DONE)

        ghosts = self.group("Ghosts : Fantômes à la maison")
        states = sorted(ghosts.lines.values_list("state", flat=True))
        self.assertEqual(states.count(ImportLine.IMPORTED), 3)
        self.assertEqual(states.count(ImportLine.EXISTING), 1)
        mock_movie.assert_called_once()
        self.assertEqual(mock_movie.call_args.args[1], "300")
        self.assertEqual(
            self.group("Frankenstein").lines.get().state,
            ImportLine.EXISTING,
        )
        self.assertEqual(mock_episode.call_count, 4)

    def test_rematch(self, _):
        """A group can be pointed to another TMDB show."""
        runner.match_import(self.history_import)
        group = self.group("Introuvable")
        client = FakeTmdb()
        runner.rematch_group(client, group, "tv", SHOWS["200"], None)
        group.refresh_from_db()
        self.assertEqual(group.media_id, "200")
        self.assertTrue(group.selected)
        self.assertFalse(group.lines.filter(state=ImportLine.MATCHED).exists())


class PlexTests(TestCase):
    """Reading a Plex server."""

    def response(self, data, status=200):
        """Return a fake HTTP response."""
        response = MagicMock(status_code=status, ok=status < 400)  # noqa: PLR2004
        response.json.return_value = {"MediaContainer": data}
        return response

    def test_entries(self):
        """History items become entries with their TMDB ids."""
        client = PlexClient("http://plex:32400/", "token")
        pages = {
            "/accounts": {
                "Account": [{"id": 1, "name": "Nicolas"}, {"id": 0, "name": ""}]
            },
            "/status/sessions/history/all": {
                "totalSize": 2,
                "Metadata": [
                    {
                        "type": "episode",
                        "grandparentTitle": "Ghosts",
                        "grandparentKey": "/library/metadata/10",
                        "parentIndex": 2,
                        "index": 1,
                        "title": "Retour",
                        "viewedAt": 1792000000,
                    },
                    {
                        "type": "movie",
                        "title": "Frankenstein",
                        "year": 2025,
                        "key": "/library/metadata/20",
                        "viewedAt": 1792100000,
                    },
                ],
            },
            "/library/metadata/10": {"Metadata": [{"Guid": [{"id": "tmdb://100"}]}]},
            "/library/metadata/20": {"Metadata": [{"Guid": [{"id": "imdb://tt1"}]}]},
        }
        client.session.get = MagicMock(
            side_effect=lambda url, **_: self.response(
                pages[url.removeprefix("http://plex:32400")],
            ),
        )
        self.assertEqual(client.accounts(), [("1", "Nicolas")])
        show, movie = plex_entries(client, "1")
        self.assertEqual(
            (show.kind, show.name, show.season, show.episode_number, show.tmdb_id),
            ("tv", "Ghosts", 2, 1, "100"),
        )
        self.assertEqual((movie.kind, movie.year, movie.tmdb_id), ("movie", 2025, ""))
        self.assertIn(
            "X-Plex-Token",
            client.session.headers,
        )

    def test_unauthorized(self):
        """A wrong token raises a clear error."""
        client = PlexClient("http://plex:32400", "bad")
        client.session.get = MagicMock(return_value=self.response({}, status=401))
        with self.assertRaises(PlexError):
            client.accounts()


class ViewTests(TestCase):
    """The import pages."""

    def setUp(self):
        """Create an administrator and a limited member."""
        user_model = get_user_model()
        self.nicolas = user_model.objects.create_superuser(
            username="nicolas",
            password="12345",  # noqa: S106
        )
        self.meilin = user_model.objects.create_user(
            username="meilin",
            password="12345",  # noqa: S106
        )
        self.client.force_login(self.nicolas)

    def upload(self, content, source="netflix", user=None):
        """Post an export file."""
        return self.client.post(
            reverse("sosoblue:history_start"),
            {
                "source": source,
                "user": (user or self.nicolas).id,
                "file": SimpleUploadedFile("history.csv", content.encode()),
            },
        )

    @patch("sosoblue.views_history.tasks.match_history_import.delay")
    def test_start_for_another_member(self, mock_delay):
        """An administrator can import into another account."""
        self.assertEqual(
            self.client.get(reverse("sosoblue:history_list")).status_code, 200
        )
        response = self.upload(NETFLIX_CSV, user=self.meilin)
        history_import = HistoryImport.objects.get()
        self.assertRedirects(
            response,
            reverse("sosoblue:history_detail", args=[history_import.id]),
            fetch_redirect_response=False,
        )
        self.assertEqual(history_import.user, self.meilin)
        mock_delay.assert_called_once_with(history_import.id)

    @patch("sosoblue.views_history.tasks.match_history_import.delay")
    def test_start_refused(self, mock_delay):
        """Wrong files and other members' accounts are refused."""
        self.upload(PRIME_CSV, source="netflix")
        self.assertFalse(HistoryImport.objects.exists())

        self.client.force_login(self.meilin)
        self.assertEqual(self.upload(NETFLIX_CSV, user=self.nicolas).status_code, 400)
        mock_delay.assert_not_called()

    @patch("sosoblue.views_history.PlexClient")
    def test_plex_accounts(self, mock_client):
        """The Plex profiles are listed to choose from."""
        mock_client.return_value.accounts.return_value = [("1", "Nicolas")]
        response = self.client.post(
            reverse("sosoblue:plex_accounts"),
            {"plex_url": "http://plex:32400", "plex_token": "token"},
        )
        self.assertContains(response, "Nicolas")

        mock_client.return_value.accounts.side_effect = PlexError("unauthorized")
        response = self.client.post(
            reverse("sosoblue:plex_accounts"),
            {"plex_url": "http://plex:32400", "plex_token": "bad"},
        )
        self.assertContains(response, "unauthorized")

    @patch("sosoblue.views_history.tasks.apply_history_import.delay")
    @patch("sosoblue.history.runner.ratings.get_rating", return_value=None)
    @patch("sosoblue.views_history.TmdbClient", FakeTmdb)
    @patch("sosoblue.history.runner.TmdbClient", FakeTmdb)
    def test_review(self, _, mock_apply):
        """The review page lists groups that can be toggled, fixed and imported."""
        history_import = HistoryImport.objects.create(
            user=self.nicolas,
            created_by=self.nicolas,
            source="netflix",
            raw_data=NETFLIX_CSV,
        )
        runner.match_import(history_import)
        url = reverse("sosoblue:history_detail", args=[history_import.id])
        response = self.client.get(url)
        self.assertContains(response, "Ghosts : Fantômes à la maison")
        self.assertContains(response, "Not found on TMDB")

        group = ImportGroup.objects.get(name="Frankenstein")
        self.client.post(
            reverse(
                "sosoblue:history_group_toggle", args=[history_import.id, group.id]
            ),
        )
        group.refresh_from_db()
        self.assertFalse(group.selected)

        group = ImportGroup.objects.get(name="Introuvable")
        response = self.client.post(
            reverse("sosoblue:history_group_fix", args=[history_import.id, group.id]),
            {"reference": "https://www.themoviedb.org/tv/200-ghosts"},
        )
        self.assertContains(response, "sb-check-on")
        group.refresh_from_db()
        self.assertEqual(group.media_id, "200")

        response = self.client.post(
            reverse("sosoblue:history_apply", args=[history_import.id]),
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        mock_apply.assert_called_once_with(history_import.id)

        response = self.client.get(url, {"progress": 1}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "Importing")

    def test_private_to_members(self):
        """Another non-admin member cannot see an import."""
        history_import = HistoryImport.objects.create(
            user=self.nicolas,
            created_by=self.nicolas,
            source="netflix",
            status=HistoryImportStatus.DONE,
        )
        self.client.force_login(self.meilin)
        url = reverse("sosoblue:history_detail", args=[history_import.id])
        self.assertEqual(self.client.get(url).status_code, 404)


class UndoTests(TestCase):
    """Undoing an import removes only what it added."""

    def setUp(self):
        """Mock TMDB and create a user with an earlier view."""
        patcher = patch("sosoblue.history.runner.anime.load_mapping", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        from sosoblue.tests.test_household import (  # noqa: PLC0415
            fake_movie,
            fake_tv,
            fake_tv_with_seasons,
        )

        for target, fake in (
            ("app.providers.tmdb.tv", fake_tv),
            ("app.providers.tmdb.tv_with_seasons", fake_tv_with_seasons),
            ("app.providers.tmdb.movie", fake_movie),
        ):
            patcher = patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user = get_user_model().objects.create_superuser(
            username="nicolas",
            password="12345",  # noqa: S106
        )
        self.before = aware(2026, 1, 1, 20, 0)
        household.mark_episode(self.user, "1399", 1, 1, self.before)

    def test_undo(self):
        """Imported episodes and movies go, earlier views stay."""
        from app.models import TV, Episode, Movie  # noqa: PLC0415

        history_import = HistoryImport.objects.create(
            user=self.user,
            created_by=self.user,
            source="netflix",
            status=HistoryImportStatus.REVIEW,
        )
        show = ImportGroup.objects.create(
            history_import=history_import,
            kind="tv",
            name="Show",
            media_type="tv",
            media_id="1399",
        )
        other = ImportGroup.objects.create(
            history_import=history_import,
            kind="tv",
            name="Other",
            media_type="tv",
            media_id="1400",
        )
        movie = ImportGroup.objects.create(
            history_import=history_import,
            kind="movie",
            name="Movie",
            media_type="movie",
            media_id="603",
        )
        when = aware(2026, 10, 1, 20, 0)
        for group, season, episode in (
            (show, 1, 1),
            (show, 1, 2),
            (other, 1, 1),
            (movie, None, None),
        ):
            ImportLine.objects.create(
                group=group,
                raw="x",
                watched_at=when,
                matched_season=season,
                matched_episode=episode,
                state=ImportLine.MATCHED,
            )
        runner.apply_import(history_import)
        self.assertEqual(Episode.objects.count(), 3)
        self.assertEqual(Movie.objects.count(), 1)

        self.client.force_login(self.user)
        with patch("sosoblue.views_history.tasks.undo_history_import.delay") as delay:
            self.client.post(reverse("sosoblue:history_undo", args=[history_import.id]))
            delay.assert_called_once_with(history_import.id)
        history_import.refresh_from_db()
        self.assertEqual(history_import.status, HistoryImportStatus.UNDOING)

        runner.undo_import(history_import)
        history_import.refresh_from_db()
        self.assertEqual(history_import.status, HistoryImportStatus.UNDONE)
        self.assertEqual(
            list(Episode.objects.values_list("item__episode_number", "end_date")),
            [(1, self.before)],
        )
        self.assertEqual(
            list(TV.objects.values_list("item__media_id", flat=True)),
            ["1399"],
        )
        self.assertFalse(Movie.objects.exists())
        response = self.client.get(
            reverse("sosoblue:history_detail", args=[history_import.id]),
        )
        self.assertContains(response, "undone")
