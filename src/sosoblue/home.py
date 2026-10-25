"""Blocks at the top of the home page: shows to resume and this week's releases."""

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass

import requests
from django.db.models import F, Max
from django.utils import timezone

from app.models import TV, MediaTypes, Sources, Status
from app.providers import services, tmdb
from events.models import Event, SentinelDatetime
from sosoblue import household

logger = logging.getLogger(__name__)

RESUME_LIMIT = 8
WEEK_DAYS = 7
STILL_URL = "https://image.tmdb.org/t/p/w780"


@dataclass
class ResumeShow:
    """A show in progress and the next episode to watch."""

    tv: TV
    season: int
    episode: int
    episode_title: str
    image: str
    watched: int
    aired: int

    @property
    def item(self):
        """Return the show item."""
        return self.tv.item

    @property
    def percent(self):
        """Return the share of aired episodes watched."""
        return round(100 * self.watched / self.aired) if self.aired else 0


def aired_episodes(media_ids):
    """Return {media_id: {(season, episode)}} of the episodes already out."""
    aired = defaultdict(set)
    events = Event.objects.filter(
        item__source=Sources.TMDB.value,
        item__media_type=MediaTypes.SEASON.value,
        item__media_id__in=media_ids,
        item__season_number__gt=0,
        content_number__isnull=False,
        datetime__lte=timezone.now(),
    ).values_list("item__media_id", "item__season_number", "content_number")
    for media_id, season, episode in events:
        aired[media_id].add((season, episode))
    return aired


def _episode_details(media_id, season, episode, fallback_image):
    """Return (title, image) of an episode, from the cached TMDB metadata."""
    try:
        metadata = tmdb.tv_with_seasons(media_id, [season])[f"season/{season}"]
    except (services.ProviderAPIError, requests.RequestException, KeyError):
        logger.warning("No metadata for %s S%sE%s", media_id, season, episode)
        return "", fallback_image
    for data in metadata.get("episodes", []):
        if data.get("episode_number") == episode:
            still = data.get("still_path")
            title = data.get("title") or data.get("name") or ""
            return title, f"{STILL_URL}{still}" if still else fallback_image
    return "", fallback_image


def next_episode(watched, aired):
    """Return the next (season, episode) to watch, or None if up to date.

    The episode after the last one watched, or the first one missed.
    """
    missing = sorted(aired - watched)
    if not missing:
        return None
    if watched:
        last = max(watched)
        after = [key for key in missing if key > last]
        if after:
            return after[0]
    return missing[0]


def resume_shows(user, limit=RESUME_LIMIT):
    """Return the shows in progress with an episode to watch, latest first."""
    tvs = list(
        TV.objects.filter(
            user=user,
            status=Status.IN_PROGRESS.value,
            item__source=Sources.TMDB.value,
        )
        .select_related("item")
        .annotate(sb_last_watched=Max("seasons__episodes__end_date"))
        .order_by(F("sb_last_watched").desc(nulls_last=True))[: limit * 2],
    )
    aired = aired_episodes([tv.item.media_id for tv in tvs])

    shows = []
    for tv in tvs:
        media_id = tv.item.media_id
        watched = set(household.watched_episodes(user, media_id))
        found = next_episode(watched, aired[media_id])
        if found is None:
            continue
        title, image = _episode_details(media_id, *found, tv.item.image)
        shows.append(
            ResumeShow(
                tv=tv,
                season=found[0],
                episode=found[1],
                episode_title=title,
                image=image,
                watched=len(watched & aired[media_id]),
                aired=len(aired[media_id]),
            ),
        )
        if len(shows) >= limit:
            break
    return shows


def week_releases(user):
    """Return [(day, [events])] of the user's releases for the next 7 days."""
    today = timezone.localdate()
    events = Event.objects.get_user_events(
        user,
        today,
        today + datetime.timedelta(days=WEEK_DAYS - 1),
    )
    sentinel = SentinelDatetime.max_datetime()
    days = defaultdict(list)
    for event in events:
        if event.datetime >= sentinel:
            continue
        days[timezone.localtime(event.datetime).date()].append(event)
    return sorted(days.items())
