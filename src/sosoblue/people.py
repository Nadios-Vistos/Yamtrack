"""Actors and crew from TMDB: search by name, and filmography."""

import logging
import re
import unicodedata

import requests
from django.core.cache import cache
from django.utils.translation import pgettext_lazy

from app.models import MediaTypes, Sources
from app.providers import services, tmdb

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60 * 60 * 24
MAX_MATCHES = 6
# TMDB TV genres that are not fiction: talk shows, news, reality
NON_FICTION_GENRES = {10763, 10764, 10767}
# Characters given to people appearing as themselves ("Self - Guest"...)
SELF_CHARACTER = re.compile(
    r"^(self|himself|herself|themselves|lui-même|elle-même|soi-même)\b",
    re.IGNORECASE,
)

# The people worth suggesting in a movie or TV search
SEARCHED_DEPARTMENTS = {"Acting", "Directing", "Writing", "Creator"}
UNCREDITED = re.compile(r"\s*\((uncredited|non crédité\w*)\)", re.IGNORECASE)

DEPARTMENTS = {
    "Acting": pgettext_lazy("person department", "Actor"),
    "Directing": pgettext_lazy("person department", "Director"),
    "Writing": pgettext_lazy("person department", "Writer"),
    "Production": pgettext_lazy("person department", "Producer"),
    "Creator": pgettext_lazy("person department", "Creator"),
    "Sound": pgettext_lazy("person department", "Sound"),
    "Camera": pgettext_lazy("person department", "Cinematographer"),
    "Editing": pgettext_lazy("person department", "Editor"),
}
JOBS = {
    "Director": pgettext_lazy("crew job", "Director"),
    "Writer": pgettext_lazy("crew job", "Writer"),
    "Screenplay": pgettext_lazy("crew job", "Screenplay"),
    "Story": pgettext_lazy("crew job", "Story"),
    "Novel": pgettext_lazy("crew job", "Novel"),
    "Creator": pgettext_lazy("crew job", "Creator"),
    "Producer": pgettext_lazy("crew job", "Producer"),
    "Executive Producer": pgettext_lazy("crew job", "Executive producer"),
    "Original Music Composer": pgettext_lazy("crew job", "Music"),
    "Director of Photography": pgettext_lazy("crew job", "Cinematography"),
    "Editor": pgettext_lazy("crew job", "Editing"),
}


def _words(text):
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return "".join(char if char.isalnum() else " " for char in text).split()


def name_matches(query, name):
    """Return whether every word of the query starts a word of the name.

    TMDB also matches other spellings and partial words; this keeps title
    searches ("The Office") from showing unrelated people.
    """
    name_words = _words(name)
    return all(
        any(word.startswith(part) for word in name_words) for part in _words(query)
    )


def department_label(department):
    """Return the translated name of a TMDB department."""
    return DEPARTMENTS.get(department, department)


def roles_label(roles):
    """Return the roles of a credit, crew jobs translated."""
    return ", ".join(str(JOBS.get(role, role)) for role in roles)


def _get(path, **params):
    """Return a TMDB response, or None when TMDB fails."""
    try:
        return services.api_request(
            Sources.TMDB.value,
            "GET",
            f"{tmdb.base_url}{path}",
            params={**tmdb.base_params, **params},
        )
    except requests.exceptions.RequestException:
        logger.warning("TMDB request failed: %s", path, exc_info=True)
        return None


def search(query):
    """Return the people whose name matches the query, most popular first."""
    query = " ".join(query.split())
    if len(query) < 2:  # noqa: PLR2004
        return []
    cache_key = f"sosoblue_people_search_{query.lower()}"
    matches = cache.get(cache_key)
    if matches is not None:
        return matches

    response = _get("/search/person", query=query)
    if response is None:
        return []
    matches = [
        {
            "id": person["id"],
            "name": person["name"],
            "image": tmdb.get_image_url(person.get("profile_path")),
            "department": person.get("known_for_department", ""),
            "known_for": [
                tmdb.get_title(work)
                for work in person.get("known_for", [])
                if work.get("title") or work.get("name")
            ][:3],
        }
        for person in response.get("results", [])
        if not person.get("adult")
        and person.get("known_for")
        and person.get("profile_path")
        and person.get("known_for_department") in SEARCHED_DEPARTMENTS
        and name_matches(query, person["name"])
    ][:MAX_MATCHES]
    cache.set(cache_key, matches, CACHE_SECONDS)
    return matches


def _credit_item(credit):
    """Return a credit in the shape of a search result."""
    is_movie = credit["media_type"] == "movie"
    media_type = MediaTypes.MOVIE.value if is_movie else MediaTypes.TV.value
    return {
        "media_id": credit["id"],
        "source": Sources.TMDB.value,
        "media_type": media_type,
        "title": tmdb.get_title(credit),
        "image": tmdb.get_image_url(credit.get("poster_path")),
        "release_year": tmdb.get_release_year(credit),
    }


def _is_fiction(credit):
    """Return whether a credit is a role in a work of fiction."""
    if NON_FICTION_GENRES & set(credit.get("genre_ids") or []):
        return False
    return not SELF_CHARACTER.match((credit.get("character") or "").strip())


def _roles(combined_credits, department):
    """Group the credits by work, keeping the roles of the person."""
    works = {}
    for credit in combined_credits.get("cast", []):
        if credit.get("media_type") not in ("movie", "tv") or not _is_fiction(credit):
            continue
        work = works.setdefault(
            (credit["media_type"], credit["id"]),
            {
                "credit": credit,
                "roles": [],
            },
        )
        character = UNCREDITED.sub("", credit.get("character") or "").strip()
        if character and character not in work["roles"]:
            work["roles"].append(character)
    if department != "Acting":
        # Directors, writers...: their own job counts, acting comes on top
        for credit in combined_credits.get("crew", []):
            if (
                credit.get("media_type") not in ("movie", "tv")
                or credit.get("department") != department
            ):
                continue
            work = works.setdefault(
                (credit["media_type"], credit["id"]),
                {
                    "credit": credit,
                    "roles": [],
                },
            )
            job = credit.get("job")
            if job and job not in work["roles"]:
                work["roles"].append(job)
    return works.values()


def _sort_key(work):
    credit = work["credit"]
    # Works without a date (never aired pilots...) go last
    return credit.get("release_date") or credit.get("first_air_date") or ""


def person(person_id):
    """Return a person and their movies and TV shows, newest first.

    Returns None when TMDB does not know the person.
    """
    cache_key = f"sosoblue_person_{person_id}_{tmdb.base_params['language']}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    response = _get(f"/person/{person_id}", append_to_response="combined_credits")
    if response is None or "id" not in response:
        return None

    department = response.get("known_for_department", "")
    movies, shows = [], []
    for work in sorted(
        _roles(response.get("combined_credits", {}), department),
        key=_sort_key,
        reverse=True,
    ):
        credit = work["credit"]
        entry = {
            "item": _credit_item(credit),
            "roles": work["roles"],
            "episodes": credit.get("episode_count"),
            "popularity": credit.get("popularity") or 0,
        }
        (movies if credit["media_type"] == "movie" else shows).append(entry)

    biography = response.get("biography", "")
    if not biography and not tmdb.base_params["language"].startswith("en"):
        english = _get(f"/person/{person_id}", language="en-US") or {}
        biography = english.get("biography", "")

    data = {
        "id": response["id"],
        "name": response.get("name", ""),
        "image": tmdb.get_image_url(response.get("profile_path")),
        "biography": biography,
        "birthday": response.get("birthday"),
        "deathday": response.get("deathday"),
        "place_of_birth": response.get("place_of_birth") or "",
        "department": department,
        "movies": movies,
        "shows": shows,
    }
    cache.set(cache_key, data, CACHE_SECONDS)
    return data
