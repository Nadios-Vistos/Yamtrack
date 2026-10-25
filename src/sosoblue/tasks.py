import logging

from celery import shared_task

from app.models import Item
from sosoblue import ratings, series
from sosoblue.history import runner
from sosoblue.models import HistoryImport

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


@shared_task(name="Match viewing history")
def match_history_import(import_id):
    """Match a Netflix, Prime Video or Plex history with TMDB."""
    history_import = HistoryImport.objects.get(id=import_id)
    runner.match_import(history_import)
    return f"Matched {history_import.total} titles"


@shared_task(name="Import viewing history")
def apply_history_import(import_id):
    """Import the reviewed lines of a viewing history."""
    history_import = HistoryImport.objects.get(id=import_id)
    runner.apply_import(history_import)
    return f"Imported {history_import.progress} lines"


@shared_task(name="Undo viewing history import")
def undo_history_import(import_id):
    """Remove what a viewing history import added."""
    history_import = HistoryImport.objects.get(id=import_id)
    runner.undo_import(history_import)
    return f"Undid {history_import.progress} titles"
