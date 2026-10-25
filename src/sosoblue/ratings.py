"""Age ratings of movies and TV shows, from TMDB."""

import logging
import re

from app.models import MediaTypes, Sources
from app.providers import services, tmdb
from sosoblue.models import ContentRating

logger = logging.getLogger(__name__)

RATED_TYPES = (MediaTypes.TV.value, MediaTypes.MOVIE.value)

# Countries tried in order. France first since its ratings are the ones the
# household knows; the others fill in when TMDB has no French rating.
COUNTRIES = ("FR", "US", "GB", "DE")

NAMED_AGES = {
    "FR": {"U": 0, "TP": 0, "TOUS PUBLICS": 0},
    "US": {
        "TV-Y": 0,
        "TV-G": 0,
        "TV-Y7": 7,
        "TV-PG": 10,
        "TV-14": 14,
        "TV-MA": 18,
        "G": 0,
        "PG": 10,
        "PG-13": 13,
        "R": 17,
        "NC-17": 18,
    },
    "GB": {"U": 0, "PG": 8, "12A": 12},
}
UNRATED = {"", "NR", "NOT RATED", "UNRATED"}
DIGITS = re.compile(r"(\d{1,2})")


def parse_age(country, certification):
    """Return the minimum age of a certification, or None if not rated."""
    value = (certification or "").strip().upper()
    if value in UNRATED:
        return None
    named = NAMED_AGES.get(country, {})
    if value in named:
        return named[value]
    match = DIGITS.search(value)
    if match:
        return int(match.group(1))
    return None


def pick_rating(certifications):
    """Pick the best (country, certification, age) from {country: [certs]}."""
    countries = list(COUNTRIES) + sorted(set(certifications) - set(COUNTRIES))
    for country in countries:
        for certification in certifications.get(country, []):
            age = parse_age(country, certification)
            if age is not None:
                return country, certification, age
    return "", "", None


def fetch_certifications(media_type, media_id):
    """Return {country: [certifications]} from TMDB."""
    if media_type == MediaTypes.TV.value:
        url = f"{tmdb.base_url}/tv/{media_id}/content_ratings"
    else:
        url = f"{tmdb.base_url}/movie/{media_id}/release_dates"
    response = services.api_request(
        Sources.TMDB.value,
        "GET",
        url,
        params={"api_key": tmdb.base_params["api_key"]},
    )

    certifications = {}
    for result in response.get("results", []):
        country = result.get("iso_3166_1", "")
        if media_type == MediaTypes.TV.value:
            values = [result.get("rating", "")]
        else:
            values = [
                release.get("certification", "")
                for release in result.get("release_dates", [])
            ]
        certifications.setdefault(country, []).extend(v for v in values if v)
    return certifications


def get_rating(media_type, media_id, *, fetch=True):
    """Return the stored ContentRating, fetching it from TMDB when missing.

    Returns None when the media type has no rating or TMDB cannot be reached.
    """
    if media_type not in RATED_TYPES:
        return None
    media_id = str(media_id)
    rating = ContentRating.objects.filter(
        media_type=media_type,
        media_id=media_id,
    ).first()
    if rating is not None or not fetch:
        return rating

    try:
        certifications = fetch_certifications(media_type, media_id)
    except Exception:
        logger.exception(
            "Could not fetch the age rating of %s %s",
            media_type,
            media_id,
        )
        return None

    country, certification, age = pick_rating(certifications)
    rating, _ = ContentRating.objects.update_or_create(
        media_type=media_type,
        media_id=media_id,
        defaults={"country": country, "certification": certification, "age": age},
    )
    return rating


def rating_map(pairs):
    """Return {(media_type, media_id): age or None} for the stored ratings."""
    pairs = {(media_type, str(media_id)) for media_type, media_id in pairs}
    media_ids = {media_id for _, media_id in pairs}
    found = {}
    for media_type, media_id, age in ContentRating.objects.filter(
        media_id__in=media_ids,
    ).values_list("media_type", "media_id", "age"):
        if (media_type, media_id) in pairs:
            found[(media_type, media_id)] = age
    return found


def fill_missing(items):
    """Fetch the ratings of the given TMDB items that have none yet."""
    pairs = {
        (item.media_type, str(item.media_id))
        for item in items
        if item.media_type in RATED_TYPES and item.source == Sources.TMDB.value
    }
    known = set(rating_map(pairs))
    fetched = 0
    for media_type, media_id in sorted(pairs - known):
        if get_rating(media_type, media_id) is not None:
            fetched += 1
    return fetched
