from django.conf import settings
from django.db import models
from django.utils.translation import pgettext_lazy

from app.models import Item


class SeriesState(models.TextChoices):
    """Simplified production state of a TV show, derived from TMDB."""

    ONGOING = "ongoing", pgettext_lazy("tv show state", "Ongoing series")
    IN_PRODUCTION = "production", pgettext_lazy("tv show state", "In production")
    ENDED = "ended", pgettext_lazy("tv show state", "Series ended")
    CANCELED = "canceled", pgettext_lazy("tv show state", "Series canceled")


# TMDB "status" values: https://developer.themoviedb.org/reference/tv-series-details
TMDB_STATES = {
    "Returning Series": SeriesState.ONGOING,
    "In Production": SeriesState.IN_PRODUCTION,
    "Planned": SeriesState.IN_PRODUCTION,
    "Pilot": SeriesState.IN_PRODUCTION,
    "Ended": SeriesState.ENDED,
    "Canceled": SeriesState.CANCELED,
    "Cancelled": SeriesState.CANCELED,
}


def state_from_tmdb(tmdb_status):
    """Return the SeriesState matching a TMDB status, or None if unknown."""
    return TMDB_STATES.get(tmdb_status or "")


class SeriesStatus(models.Model):
    """Last known TMDB status of a tracked TV show.

    Refreshed after each calendar reload, which already fetches the show
    metadata, so this costs no extra API call in most cases.
    """

    item = models.OneToOneField(
        Item,
        on_delete=models.CASCADE,
        related_name="series_status",
    )
    tmdb_status = models.CharField(max_length=40, blank=True, default="")
    last_air_date = models.DateField(null=True, blank=True)
    next_episode_season = models.PositiveIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        verbose_name = "series status"
        verbose_name_plural = "series statuses"

    def __str__(self):
        """Return the show title and its status."""
        return f"{self.item} - {self.tmdb_status or '?'}"

    @property
    def state(self):
        """Return the simplified state of the show."""
        return state_from_tmdb(self.tmdb_status)


class HouseholdProfile(models.Model):
    """Household settings of a user, set by the administrator."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="household_profile",
    )
    age_limit = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Highest age rating this member may be shown or given.",
    )

    def __str__(self):
        """Return the username and the age limit."""
        return f"{self.user} ({self.age_limit or '-'})"


class ContentRating(models.Model):
    """Age rating of a TMDB movie or TV show, fetched once and kept."""

    media_type = models.CharField(max_length=10)
    media_id = models.CharField(max_length=36)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    certification = models.CharField(max_length=20, blank=True, default="")
    country = models.CharField(max_length=2, blank=True, default="")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            models.UniqueConstraint(
                fields=["media_type", "media_id"],
                name="sosoblue_contentrating_unique_media",
            ),
        ]

    def __str__(self):
        """Return the media and its rating."""
        return f"{self.media_type} {self.media_id}: {self.certification or '?'}"


class HistorySource(models.TextChoices):
    """Where a viewing history comes from."""

    NETFLIX = "netflix", "Netflix"
    PRIME = "prime", "Prime Video"
    PLEX = "plex", "Plex"


class HistoryImportStatus(models.TextChoices):
    """Steps of a history import."""

    MATCHING = "matching", pgettext_lazy("history import status", "Matching")
    REVIEW = "review", pgettext_lazy("history import status", "To review")
    IMPORTING = "importing", pgettext_lazy("history import status", "Importing")
    DONE = "done", pgettext_lazy("history import status", "Imported")
    FAILED = "failed", pgettext_lazy("history import status", "Failed")
    UNDOING = "undoing", pgettext_lazy("history import status", "Undoing")
    UNDONE = "undone", pgettext_lazy("history import status", "Undone")


class HistoryImport(models.Model):
    """A viewing history being matched, reviewed and imported for a user."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="history_imports",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    source = models.CharField(max_length=10, choices=HistorySource.choices)
    status = models.CharField(
        max_length=10,
        choices=HistoryImportStatus.choices,
        default=HistoryImportStatus.MATCHING,
    )
    file_name = models.CharField(max_length=255, blank=True, default="")
    raw_data = models.TextField(blank=True, default="")
    plex_url = models.CharField(max_length=255, blank=True, default="")
    plex_token = models.CharField(max_length=255, blank=True, default="")
    plex_account = models.CharField(max_length=255, blank=True, default="")
    progress = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-created_at"]

    def __str__(self):
        """Return the source, the user and the date."""
        return f"{self.get_source_display()} - {self.user} - {self.created_at:%Y-%m-%d}"


class ImportGroup(models.Model):
    """The lines of an import that belong to one show or movie."""

    history_import = models.ForeignKey(
        HistoryImport,
        on_delete=models.CASCADE,
        related_name="groups",
    )
    kind = models.CharField(max_length=10)  # tv or movie, as guessed
    name = models.CharField(max_length=500)
    media_type = models.CharField(max_length=10, blank=True, default="")
    media_id = models.CharField(max_length=36, blank=True, default="")
    media_title = models.CharField(max_length=500, blank=True, default="")
    media_image = models.URLField(max_length=500, blank=True, default="")
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    above_limit = models.BooleanField(default=False)
    selected = models.BooleanField(default=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]

    def __str__(self):
        """Return the group name and its match."""
        return f"{self.name} -> {self.media_title or '?'}"


class ImportLine(models.Model):
    """One viewing from the history."""

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    IMPORTED = "imported"
    EXISTING = "existing"
    COVERED = "covered"  # unmatched, but its whole season was watched

    group = models.ForeignKey(
        ImportGroup, on_delete=models.CASCADE, related_name="lines"
    )
    raw = models.CharField(max_length=1000)
    season = models.PositiveIntegerField(null=True, blank=True)
    episode_title = models.CharField(max_length=500, blank=True, default="")
    episode_hint = models.PositiveIntegerField(null=True, blank=True)
    watched_at = models.DateTimeField()
    matched_season = models.PositiveIntegerField(null=True, blank=True)
    matched_episode = models.PositiveIntegerField(null=True, blank=True)
    state = models.CharField(max_length=10, default=UNMATCHED)

    class Meta:
        """Meta options for the model."""

        ordering = ["watched_at"]

    def __str__(self):
        """Return the raw line."""
        return self.raw
