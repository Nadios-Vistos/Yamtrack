import logging

from celery import shared_task

from sosoblue import series

logger = logging.getLogger(__name__)


@shared_task(name="Refresh series status")
def refresh_series_status():
    """Refresh the TMDB status of every tracked TV show."""
    return f"Refreshed the status of {series.refresh_series_status()} TV shows"
