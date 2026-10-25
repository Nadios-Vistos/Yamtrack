import logging

from celery.signals import task_postrun

from sosoblue import series

logger = logging.getLogger(__name__)

CALENDAR_TASK = "Reload calendar"


@task_postrun.connect
def refresh_after_calendar_reload(sender=None, kwargs=None, state=None, **_):
    """Refresh show statuses once the calendar reload has fetched them.

    The calendar reload runs every day for all items, and right after a show
    is added for that show only, so statuses follow without a schedule of
    their own.
    """
    if getattr(sender, "name", None) != CALENDAR_TASK or state != "SUCCESS":
        return

    items = (kwargs or {}).get("items_to_process")
    try:
        series.refresh_series_status(items)
    except Exception:
        logger.exception("Could not refresh the status of TV shows")
