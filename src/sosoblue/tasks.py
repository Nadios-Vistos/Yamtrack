import logging

from celery import shared_task

from app.models import Item
from sosoblue import ratings, series

logger = logging.getLogger(__name__)


@shared_task(name="Refresh series status")
def refresh_series_status():
    """Refresh the TMDB status of every tracked TV show."""
    return f"Refreshed the status of {series.refresh_series_status()} TV shows"


@shared_task(name="Fill content ratings")
def fill_content_ratings(item_ids):
    """Fetch the age ratings of the given items that have none yet."""
    items = Item.objects.filter(id__in=item_ids)
    return f"Fetched {ratings.fill_missing(items)} age ratings"
