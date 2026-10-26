"""Animation filter of the TV show list."""

from django.core.cache import cache
from django.utils.translation import pgettext_lazy

from app.models import MediaTypes
from sosoblue.models import SeriesStatus

ALL = "all"
ANIMATION = "animation"
LIVE = "live"
FILTERS = {
    ALL: pgettext_lazy("animation filter", "All"),
    ANIMATION: pgettext_lazy("animation filter", "Animation"),
    LIVE: pgettext_lazy("animation filter", "Live action"),
}
SESSION_KEY = "sosoblue_tv_genre"
BACKFILL_KEY = "sosoblue_genres_backfill"


def current(request):
    """Return the filter of the TV list, remembered for the session.

    It is kept server side so pages, sorting and searching keep it without
    carrying it in every link.
    """
    chosen = request.GET.get("genre")
    if chosen in FILTERS:
        request.session[SESSION_KEY] = chosen
        return chosen
    return request.session.get(SESSION_KEY, ALL)


def apply(request, media_type, queryset):
    """Filter a TV list on animation."""
    if media_type != MediaTypes.TV.value:
        return queryset
    chosen = current(request)
    if chosen == ANIMATION:
        return queryset.filter(item__series_status__is_animation=True)
    if chosen == LIVE:
        return queryset.exclude(item__series_status__is_animation=True)
    return queryset


def missing_count():
    """Return how many tracked shows have no genres yet."""
    from sosoblue import series  # noqa: PLC0415

    known = SeriesStatus.objects.filter(is_animation__isnull=False).values("item_id")
    return series.tracked_tv_items().exclude(id__in=known).count()


def backfill_if_needed():
    """Fetch the genres of the shows that have none, at most once an hour.

    Returns whether a fetch was just started. Shows TMDB no longer knows
    stay without genres, so this does not insist more than once an hour.
    """
    from sosoblue import tasks  # noqa: PLC0415

    if not missing_count() or not cache.add(BACKFILL_KEY, value=1, timeout=60 * 60):
        return False
    tasks.refresh_series_status.delay()
    return True
