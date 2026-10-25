import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import translation
from django.utils.translation import gettext as _

from app.models import Item, UserMessage, UserMessageLevel
from sosoblue import anime, household, ratings, series
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


@shared_task(name="Move anime to the anime section")
def convert_anime(requested_by_id):
    """Move the anime among the members' TV shows to the Anime section."""
    requested_by = get_user_model().objects.get(id=requested_by_id)
    mapping = anime.load_mapping()
    with translation.override(requested_by.language or None):
        if mapping is None:
            UserMessage.objects.create(
                user=requested_by,
                level=UserMessageLevel.ERROR,
                message=_("The anime list could not be downloaded. Try again later."),
            )
            return "No mapping"

        report = []
        for member in household.members():
            if not member.user.anime_enabled:
                continue
            moved = anime.convert_user_shows(member.user, mapping)
            if moved:
                report.append(f"{member.name}: {', '.join(sorted(moved))}")
        if report:
            message = _("Anime moved to the Anime section. %(report)s") % {
                "report": " — ".join(report),
            }
        else:
            message = _("No anime found among the series.")
        UserMessage.objects.create(
            user=requested_by,
            level=UserMessageLevel.SUCCESS,
            message=message,
        )
    return message
