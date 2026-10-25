from django import template

from app.models import MediaTypes, Sources
from sosoblue import series
from sosoblue.models import SeriesState, state_from_tmdb

register = template.Library()

_CACHE_ATTR = "_sosoblue_series_states"


def _value(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


@register.simple_tag(takes_context=True)
def series_state(context, item):
    """Return the SeriesState of a TMDB TV item, or None.

    The statuses are loaded once per request, so a page full of cards costs
    a single query.
    """
    if (
        _value(item, "media_type") != MediaTypes.TV.value
        or _value(item, "source") != Sources.TMDB.value
    ):
        return None

    request = context.get("request")
    states = getattr(request, _CACHE_ATTR, None) if request is not None else None
    if states is None:
        states = series.status_map()
        if request is not None:
            setattr(request, _CACHE_ATTR, states)
    return states.get(str(_value(item, "media_id")))


@register.filter
def tmdb_series_state(tmdb_status):
    """Return the SeriesState matching a raw TMDB status (or a SeriesState)."""
    if tmdb_status in SeriesState.values:
        return SeriesState(tmdb_status)
    return state_from_tmdb(tmdb_status)


@register.filter
def series_state_label(state):
    """Return the translated label of a SeriesState."""
    try:
        return SeriesState(state).label
    except ValueError:
        return ""


@register.simple_tag(takes_context=True)
def detail_hero(context, media):
    """Return what the mobile header of a media page shows, or None."""
    from sosoblue import artwork, home, household, ratings  # noqa: PLC0415

    media_type = _value(media, "media_type")
    if _value(media, "source") != Sources.TMDB.value or media_type not in (
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
        MediaTypes.MOVIE.value,
    ):
        return None
    media_id = str(_value(media, "media_id"))
    show_type = MediaTypes.MOVIE.value if media_type == "movie" else "tv"
    details = _value(media, "details") or {}
    rating = ratings.get_rating(show_type, media_id, fetch=False)
    hero = {
        "backdrop": artwork.backdrop(show_type, media_id),
        "year": str(
            details.get("first_air_date") or details.get("release_date") or "",
        )[:4],
        "certification": rating.certification if rating else "",
        "next": None,
    }

    request = context.get("request")
    if show_type == "tv" and request is not None and request.user.is_authenticated:
        watched = set(household.watched_episodes(request.user, media_id))
        aired = home.aired_episodes([media_id])[media_id]
        if watched:
            hero["next"] = home.next_episode(watched, aired)
    return hero
