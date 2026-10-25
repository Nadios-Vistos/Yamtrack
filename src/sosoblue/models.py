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
