"""TV show status and the list of shows waiting for new episodes."""

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from app.models import TV, Episode, Item, MediaTypes, Sources, Status
from app.providers import services, tmdb
from events.models import Event, SentinelDatetime
from sosoblue.models import SeriesState, SeriesStatus, state_from_tmdb

logger = logging.getLogger(__name__)

WAITING_STATES = {SeriesState.ONGOING, SeriesState.IN_PRODUCTION}


def tracked_tv_items():
    """Return the TMDB TV items tracked by at least one user."""
    return Item.objects.filter(
        media_type=MediaTypes.TV.value,
        source=Sources.TMDB.value,
        tv__isnull=False,
    ).distinct()


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def refresh_series_status(items=None):
    """Store the current TMDB status of the given TV items.

    Without items, refresh every tracked show. Returns the number of shows
    updated. TMDB responses are cached for a day by the provider, and the
    calendar reload fetches the same data just before, so this is cheap.
    """
    if items is None:
        items = tracked_tv_items()
    else:
        items = [
            item
            for item in items
            if item.media_type == MediaTypes.TV.value
            and item.source == Sources.TMDB.value
        ]

    updated = 0
    for item in items:
        try:
            metadata = tmdb.tv(item.media_id)
        except services.ProviderAPIError:
            logger.warning("Could not fetch the status of %s", item)
            continue
        except Exception:
            logger.exception("Error while fetching the status of %s", item)
            continue

        details = metadata.get("details", {})
        SeriesStatus.objects.update_or_create(
            item=item,
            defaults={
                "tmdb_status": details.get("status") or "",
                "last_air_date": _parse_date(details.get("last_air_date")),
                "next_episode_season": metadata.get("next_episode_season"),
            },
        )
        updated += 1

    logger.info("Refreshed the status of %d TV shows", updated)
    return updated


def status_map(media_ids=None):
    """Return {media_id: SeriesState} for TMDB shows with a known status."""
    queryset = SeriesStatus.objects.filter(item__source=Sources.TMDB.value)
    if media_ids is not None:
        queryset = queryset.filter(item__media_id__in=list(media_ids))
    return {
        media_id: state
        for media_id, tmdb_status in queryset.values_list(
            "item__media_id",
            "tmdb_status",
        )
        if (state := state_from_tmdb(tmdb_status))
    }


@dataclass
class WaitingShow:
    """A show the user is up to date on, waiting for what comes next."""

    tv: TV
    state: SeriesState | None
    next_datetime: datetime.datetime | None = None
    next_season: int | None = None
    next_episode: int | None = None

    @property
    def item(self):
        """Return the TV item."""
        return self.tv.item


def _release_events(media_ids):
    """Return the aired episodes and the next release of each show."""
    now = timezone.now()
    aired = defaultdict(set)
    upcoming = {}
    events = (
        Event.objects.filter(
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.SEASON.value,
            item__media_id__in=media_ids,
            item__season_number__gt=0,
        )
        .select_related("item")
        .order_by("datetime")
    )
    for event in events:
        media_id = event.item.media_id
        if event.datetime <= now:
            if event.content_number is not None:
                aired[media_id].add((event.item.season_number, event.content_number))
        elif media_id not in upcoming:
            upcoming[media_id] = event
    return aired, upcoming


def _watched_episodes(user, media_ids):
    """Return the (season, episode) pairs the user watched, by show."""
    watched = defaultdict(set)
    if not media_ids:
        return watched
    episodes = Episode.objects.filter(
        related_season__user=user,
        item__source=Sources.TMDB.value,
        item__media_id__in=media_ids,
    ).values_list("item__media_id", "item__season_number", "item__episode_number")
    for media_id, season_number, episode_number in episodes:
        watched[media_id].add((season_number, episode_number))
    return watched


def _waiting_show(tv, state, event):
    """Build the WaitingShow of a show, with its next release if any."""
    show = WaitingShow(tv=tv, state=state)
    if event is not None:
        show.next_season = event.item.season_number
        show.next_episode = event.content_number
        if event.datetime < SentinelDatetime.max_datetime():
            show.next_datetime = event.datetime
    return show


def waiting_shows(user):
    """Split the user's up-to-date shows by what is known of the next episode.

    A show is up to date when it is completed, or in progress with every aired
    episode watched (Yamtrack reopens a completed show as soon as a future
    season is announced).

    Returns a dict with:
        dated: next episode has a release date, soonest first
        undated: the show continues but no date is known yet
        unknown: no status fetched yet
    """
    result = {"dated": [], "undated": [], "unknown": []}
    tvs = list(
        TV.objects.filter(
            user=user,
            item__source=Sources.TMDB.value,
            status__in=[Status.COMPLETED.value, Status.IN_PROGRESS.value],
        ).select_related("item"),
    )
    if not tvs:
        return result

    media_ids = [tv.item.media_id for tv in tvs]
    states = status_map(media_ids)
    aired, upcoming = _release_events(media_ids)
    watched = _watched_episodes(
        user,
        [tv.item.media_id for tv in tvs if tv.status == Status.IN_PROGRESS.value],
    )

    for tv in tvs:
        media_id = tv.item.media_id
        state = states.get(media_id)
        event = upcoming.get(media_id)

        if tv.status == Status.IN_PROGRESS.value and (
            event is None or not aired[media_id] <= watched[media_id]
        ):
            # Still catching up, or nothing announced: a regular show in
            # progress, not one waiting for more.
            continue
        if event is None and state not in WAITING_STATES:
            if state is None:
                result["unknown"].append(_waiting_show(tv, state, event))
            continue

        show = _waiting_show(tv, state, event)
        result["dated" if show.next_datetime else "undated"].append(show)

    result["dated"].sort(key=lambda show: show.next_datetime)
    for key in ("undated", "unknown"):
        result[key].sort(key=lambda show: str(show.item.title).lower())
    return result


def missing_status_count():
    """Return how many tracked TMDB shows have no stored status yet."""
    return tracked_tv_items().filter(Q(series_status__isnull=True)).count()
