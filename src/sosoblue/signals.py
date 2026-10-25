import logging

from celery.signals import task_postrun

from app.models import Item, MediaTypes, Sources
from sosoblue import ratings, series

logger = logging.getLogger(__name__)

CALENDAR_TASK = "Reload calendar"


def _tracked_items():
    """Return the TMDB movies and shows tracked by at least one user."""
    shows = series.tracked_tv_items()
    movies = Item.objects.filter(
        media_type=MediaTypes.MOVIE.value,
        source=Sources.TMDB.value,
        movie__isnull=False,
    ).distinct()
    return list(shows) + list(movies)


@task_postrun.connect
def refresh_after_calendar_reload(sender=None, kwargs=None, state=None, **_):
    """Refresh show statuses and age ratings after a calendar reload.

    The calendar reload runs every day for all items, and right after an
    item is added for that item only, so this needs no schedule of its own.
    Show statuses come from data the reload just cached; age ratings are
    only fetched for items that have none yet.
    """
    if getattr(sender, "name", None) != CALENDAR_TASK or state != "SUCCESS":
        return

    items = (kwargs or {}).get("items_to_process")
    try:
        series.refresh_series_status(items)
    except Exception:
        logger.exception("Could not refresh the status of TV shows")
    try:
        ratings.fill_missing(items if items is not None else _tracked_items())
    except Exception:
        logger.exception("Could not fetch age ratings")
