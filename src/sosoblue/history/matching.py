"""Find TMDB shows, movies and episodes from the titles of a viewing history.

Streaming services only give titles ("Show: Season 2: Episode title") and
dates. Shows and movies are searched on TMDB, then each episode is found by
comparing its title with the TMDB episode titles, in the configured language
first, then in English.
"""

import difflib
import logging
import re
import unicodedata

import requests
from django.conf import settings

from app.models import Sources
from app.providers import services, tmdb

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.82
CANDIDATES = 6
SAMPLE_TITLES = 40
GENERIC_EPISODE = re.compile(
    r"^(?:épisode|episode|chapitre|chapter|partie|part)\s*(\d+)$",
    re.IGNORECASE,
)
TRUNCATED = ("...", "…")


def norm(text):
    """Return a comparable form of a title: lowercase letters and digits only."""
    text = unicodedata.normalize("NFKD", (text or "").replace("\xa0", " ").lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("œ", "oe").replace("æ", "ae")
    return re.sub(r"[^a-z0-9]+", "", text)


class TmdbClient:
    """Small TMDB client with a cache for the duration of one import."""

    def __init__(self):
        """Prepare the cache."""
        self.cache = {}
        self.languages = list(dict.fromkeys([settings.TMDB_LANG, "en-US"]))

    def get(self, path, **params):
        """Return the TMDB response for a path, or None if not found."""
        params = {"api_key": tmdb.base_params["api_key"], **params}
        key = (path, tuple(sorted(params.items())))
        if key in self.cache:
            return self.cache[key]
        try:
            data = services.api_request(
                Sources.TMDB.value,
                "GET",
                f"{tmdb.base_url}/{path}",
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            if getattr(error.response, "status_code", None) != requests.codes.not_found:
                logger.warning("TMDB request failed: %s (%s)", path, error)
            data = None
        except requests.exceptions.RequestException as error:
            logger.warning("TMDB request failed: %s (%s)", path, error)
            data = None
        self.cache[key] = data
        return data

    def search(self, kind, query):
        """Return the TMDB search results for a show or movie title."""
        data = self.get(f"search/{kind}", query=query, language=self.languages[0])
        return (data or {}).get("results") or []

    def show(self, show_id):
        """Return the details of a show."""
        return self.get(f"tv/{show_id}", language=self.languages[0]) or {}

    def movie(self, movie_id):
        """Return the details of a movie."""
        return self.get(f"movie/{movie_id}", language=self.languages[0]) or {}

    def season_episodes(self, show_id, season, language=None):
        """Return the episodes of a season."""
        data = self.get(
            f"tv/{show_id}/season/{season}",
            language=language or self.languages[0],
        )
        return (data or {}).get("episodes") or []

    def season_counts(self, show_id):
        """Return {season number: episode count} of a show."""
        return {
            season["season_number"]: season.get("episode_count") or 0
            for season in self.show(show_id).get("seasons", [])
        }


def find_show(client, name, episode_titles=()):
    """Return the TMDB show matching a name, or None.

    When several shows have exactly that name, the one whose episode titles
    match the viewed ones best is chosen.
    """
    results = client.search("tv", name)
    if not results:
        return None
    target = norm(name)
    exact = [
        result
        for result in results[:CANDIDATES]
        if target in (norm(result.get("name")), norm(result.get("original_name")))
    ]
    if len(exact) <= 1 or not episode_titles:
        return exact[0] if exact else results[0]

    sample = {norm(title) for title in list(episode_titles)[:SAMPLE_TITLES]}

    def score(result):
        titles = set()
        for season in client.season_counts(result["id"]):
            titles |= {
                norm(episode.get("name"))
                for episode in client.season_episodes(result["id"], season)
            }
        return len(sample & titles)

    return max(exact, key=score)


def find_movie(client, title, watched_on="", year=None):
    """Return the TMDB movie matching a title, or None.

    Among movies with that exact title, take the given year, otherwise the
    most recent one released before it was watched.
    """
    results = client.search("movie", title)
    target = norm(title)
    exact = [
        result
        for result in results[:10]
        if target in (norm(result.get("title")), norm(result.get("original_title")))
    ]
    if year:
        same_year = [r for r in exact if (r.get("release_date") or "")[:4] == str(year)]
        if same_year:
            return same_year[0]
    if len(exact) > 1 and watched_on:
        before = [r for r in exact if (r.get("release_date") or "9999") <= watched_on]
        if before:
            return max(before, key=lambda result: result.get("release_date") or "")
    if exact:
        return exact[0]
    return results[0] if results else None


def _match_title(client, show_id, order, title):
    """Return (season, episode) whose TMDB title matches, or None."""
    target = norm(re.sub(r"(\.\.\.|…)$", "", title))
    if not target:
        return None
    truncated = title.endswith(TRUNCATED)
    for language in client.languages:
        best_score, best = 0, None
        for season in order:
            for episode in client.season_episodes(show_id, season, language):
                name = norm(episode.get("name"))
                if not name:
                    continue
                if name == target or (truncated and name.startswith(target)):
                    return season, episode["episode_number"]
                ratio = difflib.SequenceMatcher(None, name, target).ratio()
                if ratio > best_score:
                    best_score, best = ratio, (season, episode["episode_number"])
        if best_score >= FUZZY_THRESHOLD:
            return best
    return None


def match_episode(client, show_id, seasons, title, wanted_season=None):
    """Return (season, episode) of an episode title, or None."""
    title = (title or "").replace("\xa0", " ").strip()
    order = [wanted_season] if wanted_season in seasons else []
    order += [season for season in seasons if season != wanted_season]

    found = _match_title(client, show_id, order, title)
    if found is None and ": " in title:
        # "Part: Episode title" when the show name has a subtitle
        found = _match_title(client, show_id, order, title.rsplit(": ", 1)[1])
    if found is None:
        generic = GENERIC_EPISODE.match(title)
        if generic and wanted_season in seasons:
            found = (wanted_season, int(generic.group(1)))
    return found
