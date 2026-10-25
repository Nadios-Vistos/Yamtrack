"""Put Japanese anime in the Anime section instead of TV shows.

Yamtrack's Anime section holds MyAnimeList entries. Histories are matched
with TMDB, so anime would land in TV shows. AniBridge's mappings (the ones
Yamtrack's own Plex webhook uses) link a TVDB season and episode to a
MyAnimeList entry and episode; TMDB gives the TVDB id of each show.
"""

import logging
from collections import defaultdict

import requests

from app.models import TV, Anime, Item, MediaTypes, Sources, Status
from app.providers import mal, services, tmdb
from integrations.webhooks import anime_mappings
from sosoblue import household

logger = logging.getLogger(__name__)


def load_mapping():
    """Return AniBridge's mapping data, or None if it cannot be fetched."""
    try:
        return anime_mappings.fetch_mapping_data()
    except (services.ProviderAPIError, requests.RequestException, ValueError):
        logger.warning("Could not fetch the anime mappings")
        return None


def tvdb_id(media_id):
    """Return the TVDB id of a TMDB show, or None."""
    try:
        return tmdb.tv(media_id).get("tvdb_id")
    except (services.ProviderAPIError, requests.RequestException, KeyError):
        return None


def to_mal(mapping, show_tvdb_id, season, episode):
    """Return (MyAnimeList id, episode) of a TVDB episode, or None."""
    if not mapping or not show_tvdb_id or not season:
        return None
    mal_id, mal_episode = anime_mappings.get_mal_id_from_tvdb(
        mapping,
        show_tvdb_id,
        season,
        episode,
    )
    if not mal_id or not mal_episode:
        return None
    return str(mal_id), mal_episode


def mark_anime(user, mal_id, episode, watched_at, started_at=None):
    """Record that a user watched an anime up to an episode.

    Returns False when the user had already watched that far.
    """
    metadata = mal.anime(mal_id)
    item, _ = Item.objects.get_or_create(
        media_id=str(mal_id),
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        defaults={"title": metadata["title"], "image": metadata["image"]},
    )
    max_progress = metadata.get("max_progress")
    completed = bool(max_progress) and episode >= max_progress
    started_at = started_at or watched_at

    anime = Anime.objects.filter(user=user, item=item).order_by("-created_at").first()
    if anime is None:
        Anime.objects.create(
            item=item,
            user=user,
            progress=max_progress if completed else episode,
            status=Status.COMPLETED.value if completed else Status.IN_PROGRESS.value,
            start_date=started_at,
            end_date=watched_at if completed else None,
        )
        return True
    if anime.status == Status.COMPLETED.value or anime.progress >= episode:
        return False

    anime.progress = max_progress if completed else episode
    if completed:
        anime.status = Status.COMPLETED.value
        anime.end_date = watched_at
    elif anime.status != Status.IN_PROGRESS.value:
        anime.status = Status.IN_PROGRESS.value
    if anime.start_date is None or anime.start_date > started_at:
        anime.start_date = started_at
    anime.save()
    return True


def group_by_anime(mapping, show_tvdb_id, views):
    """Map {(season, episode): date} views to MyAnimeList entries.

    Returns ({mal_id: (last episode, first date, last date)}, unmapped keys).
    """
    entries = defaultdict(lambda: [0, None, None])
    unmapped = []
    for (season, episode), when in views.items():
        found = to_mal(mapping, show_tvdb_id, season, episode)
        if found is None:
            unmapped.append((season, episode))
            continue
        mal_id, mal_episode = found
        entry = entries[mal_id]
        entry[0] = max(entry[0], mal_episode)
        entry[1] = when if entry[1] is None or when < entry[1] else entry[1]
        entry[2] = when if entry[2] is None or when > entry[2] else entry[2]
    return {key: tuple(value) for key, value in entries.items()}, unmapped


def convert_user_shows(user, mapping):
    """Move the anime among a user's TV shows to the Anime section.

    A show moves only when every watched episode (specials aside) has a
    MyAnimeList match, so nothing is lost. Returns the titles moved.
    """
    moved = []
    tvs = TV.objects.filter(user=user, item__source=Sources.TMDB.value).select_related(
        "item",
    )
    for tv in tvs:
        show_tvdb_id = tvdb_id(tv.item.media_id)
        if not show_tvdb_id:
            continue
        views = {
            key: when
            for key, when in household.watched_episodes(user, tv.item.media_id).items()
            if key[0]
        }
        if not views:
            continue
        entries, unmapped = group_by_anime(mapping, show_tvdb_id, views)
        if unmapped or not entries:
            continue
        try:
            for mal_id, (episode, first, last) in entries.items():
                mark_anime(user, mal_id, episode, last, first)
        except (services.ProviderAPIError, requests.RequestException, KeyError):
            logger.warning("Could not move %s to anime", tv.item)
            continue
        moved.append(str(tv.item.title))
        tv.delete()
    return moved
