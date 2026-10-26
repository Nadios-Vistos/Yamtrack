import functools
import hashlib
from pathlib import Path

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


@register.simple_tag(takes_context=True)
def home_blocks(context):
    """Return the resume and this-week blocks of the home page."""
    from django.utils import timezone  # noqa: PLC0415

    from sosoblue import home  # noqa: PLC0415

    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return None
    return {
        "resume": home.resume_shows(request.user),
        "week": home.week_releases(request.user),
        "today": timezone.localdate(),
    }


@register.simple_tag(takes_context=True)
def household_panel_context(context, media):
    """Return the household panel of a media page, or None."""
    from sosoblue import household  # noqa: PLC0415

    request = context.get("request")
    media_type = _value(media, "media_type")
    if (
        request is None
        or not request.user.is_authenticated
        or _value(media, "source") != Sources.TMDB.value
        or media_type not in ("tv", "season", "movie")
    ):
        return None
    season = _value(media, "season_number") if media_type == "season" else None
    return household.panel(
        request.user,
        media_type,
        str(_value(media, "media_id")),
        season,
    )


@register.simple_tag(takes_context=True)
def people_matches(context):
    """Return the people matching a movie or TV search, on its first page."""
    from sosoblue import people  # noqa: PLC0415

    request = context.get("request")
    searched = context.get("media_type")
    if (
        request is None
        or searched not in (MediaTypes.TV.value, MediaTypes.MOVIE.value)
        or request.GET.get("page", "1") != "1"
    ):
        return []
    matches = people.search(request.GET.get("q", ""))
    for match in matches:
        match["department_label"] = people.department_label(match["department"])
    return matches


@register.simple_tag(takes_context=True)
def navigation_tabs(context):
    """Return the tabs of the phone tab bar, in the user's order."""
    from sosoblue import navigation  # noqa: PLC0415

    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return []
    return navigation.tab_bar(request.user, request.path, context.get("media_type"))


@register.simple_tag
def ordered_sidebar_media_types(user):
    """Return the sidebar media types in the user's order."""
    from app.templatetags.app_tags import media_type_readable_plural  # noqa: PLC0415
    from sosoblue import navigation  # noqa: PLC0415

    return [
        {
            "media_type": media_type,
            "display_name": media_type_readable_plural(media_type),
        }
        for media_type in navigation.ordered_media_types(user)
    ]


@register.simple_tag(takes_context=True)
def animation_filter(context):
    """Return the animation filter of the TV list, or None for other lists."""
    from sosoblue import genres  # noqa: PLC0415

    request = context.get("request")
    if request is None or context.get("media_type") != MediaTypes.TV.value:
        return None
    return {
        "current": genres.current(request),
        "choices": list(genres.FILTERS.items()),
        "missing": genres.backfill_if_needed(),
    }


@functools.cache
def _asset_version(path):
    """Return a short hash of a static file, read once per process."""
    from django.contrib.staticfiles import finders  # noqa: PLC0415

    found = finders.find(path)
    if not found:
        return ""
    with Path(found).open("rb") as file:
        return hashlib.md5(file.read(), usedforsecurity=False).hexdigest()[:10]


@register.simple_tag
def sb_asset(path):
    """Return the URL of a static file with its version.

    nginx lets browsers keep static files for 30 days without checking, so
    without the version an updated stylesheet would not reach them.
    """
    from django.templatetags.static import static  # noqa: PLC0415

    version = _asset_version(path)
    url = static(path)
    return f"{url}?v={version}" if version else url
