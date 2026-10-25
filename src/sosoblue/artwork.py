"""Backdrop images of shows and movies, from TMDB, cached for a week."""

import logging

import requests
from django.core.cache import cache

from app.models import MediaTypes, Sources
from app.providers import services, tmdb

logger = logging.getLogger(__name__)

BACKDROP_URL = "https://image.tmdb.org/t/p/w1280"
CACHE_SECONDS = 7 * 24 * 3600


def backdrop(media_type, media_id):
    """Return the URL of the backdrop of a TMDB show or movie, or ''."""
    if media_type not in (MediaTypes.TV.value, MediaTypes.MOVIE.value):
        return ""
    key = f"sosoblue_backdrop_{media_type}_{media_id}"
    url = cache.get(key)
    if url is None:
        url = ""
        try:
            data = services.api_request(
                Sources.TMDB.value,
                "GET",
                f"{tmdb.base_url}/{media_type}/{media_id}",
                params={"api_key": tmdb.base_params["api_key"]},
            )
            if data.get("backdrop_path"):
                url = f"{BACKDROP_URL}{data['backdrop_path']}"
        except (requests.exceptions.RequestException, ValueError, AttributeError):
            logger.warning("No backdrop for %s %s", media_type, media_id)
        cache.set(key, url, CACHE_SECONDS)
    return url
