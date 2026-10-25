"""Household features: shared viewing and the household overview.

Every active user of the instance is a member of the household. Members
can mark what they watched together for each other. An age limit, set by
an administrator, keeps content above a member's age away from them.
"""

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.utils import timezone

from app.models import TV, Episode, Item, MediaTypes, Movie, Season, Sources, Status
from app.providers import services, tmdb
from sosoblue import ratings
from sosoblue.models import HouseholdProfile

logger = logging.getLogger(__name__)

ACTIVITY_DAYS = 30
ACTIVITY_LIMIT = 6
AGE_LIMIT_CHOICES = (10, 12, 14, 16, 18)
MIN_MEMBERS = 2


@dataclass
class Member:
    """A member of the household."""

    user: object
    age_limit: int | None

    @property
    def id(self):
        """Return the user id."""
        return self.user.id

    @property
    def name(self):
        """Return the name shown to the household."""
        return self.user.first_name or self.user.username

    @property
    def initial(self):
        """Return the initial shown on the chips."""
        return self.name[:1].upper()


def members():
    """Return the household members, oldest account first."""
    users = get_user_model().objects.filter(is_active=True).order_by("id")
    limits = dict(
        HouseholdProfile.objects.filter(user__in=users).values_list(
            "user_id",
            "age_limit",
        ),
    )
    return [Member(user=user, age_limit=limits.get(user.id)) for user in users]


def get_member(user_id):
    """Return the member with this id, or None."""
    for member in members():
        if str(member.id) == str(user_id):
            return member
    return None


def age_limit_of(user):
    """Return the age limit of a user, or None."""
    return (
        HouseholdProfile.objects.filter(user=user)
        .values_list("age_limit", flat=True)
        .first()
    )


def set_age_limit(user, age_limit):
    """Store the age limit of a user (None removes it)."""
    HouseholdProfile.objects.update_or_create(
        user=user,
        defaults={"age_limit": age_limit},
    )


def is_allowed(age_limit, age, *, strict):
    """Return whether content rated `age` suits a member with `age_limit`.

    Unrated content is refused in strict mode (what a limited member sees),
    and allowed otherwise (when someone deliberately shares it with them).
    """
    if age_limit is None:
        return True
    if age is None:
        return not strict
    return age <= age_limit


def clear_tracking(user):
    """Delete every series and movie a user tracks. Returns what was deleted.

    Seasons and episodes go with their show. Other media types, lists and
    settings are left alone.
    """
    shows = TV.objects.filter(user=user)
    movies = Movie.objects.filter(user=user)
    counts = {
        "shows": shows.count(),
        "episodes": Episode.objects.filter(related_season__user=user).count(),
        "movies": movies.count(),
    }
    shows.delete()
    Season.objects.filter(user=user).delete()
    movies.delete()
    logger.info("Cleared the series and movies of %s: %s", user, counts)
    return counts


# Marking for another member


def mark_episode(user, media_id, season_number, episode_number, end_date=None):
    """Mark an episode as watched for a user. Returns False if already watched."""
    tv_metadata = tmdb.tv_with_seasons(media_id, [season_number])
    season_metadata = tv_metadata[f"season/{season_number}"]

    tv_item, _ = Item.objects.get_or_create(
        media_id=media_id,
        source=Sources.TMDB.value,
        media_type=MediaTypes.TV.value,
        defaults={"title": tv_metadata["title"], "image": tv_metadata["image"]},
    )
    tv_instance, _ = TV.objects.get_or_create(
        item=tv_item,
        user=user,
        defaults={"status": Status.IN_PROGRESS.value},
    )
    season_item, _ = Item.objects.get_or_create(
        media_id=media_id,
        source=Sources.TMDB.value,
        media_type=MediaTypes.SEASON.value,
        season_number=season_number,
        defaults={"title": tv_metadata["title"], "image": season_metadata["image"]},
    )
    season_instance, _ = Season.objects.get_or_create(
        item=season_item,
        user=user,
        related_tv=tv_instance,
        defaults={"status": Status.IN_PROGRESS.value},
    )
    episode_item = season_instance.get_episode_item(episode_number, season_metadata)
    if Episode.objects.filter(
        item=episode_item,
        related_season=season_instance,
    ).exists():
        return False

    Episode.objects.create(
        item=episode_item,
        related_season=season_instance,
        end_date=end_date or timezone.now().replace(second=0, microsecond=0),
    )
    logger.info(
        "Marked %s S%02dE%02d as watched for %s",
        tv_metadata["title"],
        season_number,
        episode_number,
        user,
    )
    return True


def unmark_episode(user, media_id, season_number, episode_number):
    """Remove the latest view of an episode for a user. Returns False if none."""
    episode = (
        Episode.objects.filter(
            related_season__user=user,
            item__media_id=media_id,
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.EPISODE.value,
            item__season_number=season_number,
            item__episode_number=episode_number,
        )
        .order_by("-end_date", "-created_at")
        .first()
    )
    if episode is None:
        return False
    episode.delete()
    logger.info(
        "Removed %s S%02dE%02d for %s",
        media_id,
        season_number,
        episode_number,
        user,
    )
    return True


def watched_episodes(user, media_id, season_number=None):
    """Return {(season, episode): end_date} of the episodes a user watched."""
    queryset = Episode.objects.filter(
        related_season__user=user,
        item__media_id=media_id,
        item__source=Sources.TMDB.value,
        item__media_type=MediaTypes.EPISODE.value,
    )
    if season_number is not None:
        queryset = queryset.filter(item__season_number=season_number)
    watched = {}
    for season, episode, end_date in queryset.order_by("end_date").values_list(
        "item__season_number",
        "item__episode_number",
        "end_date",
    ):
        watched[(season, episode)] = end_date
    return watched


def align_episodes(source_user, target_user, media_id):
    """Mark for the target every episode of a show the source watched."""
    theirs = watched_episodes(target_user, media_id)
    missing = sorted(
        (key, end_date)
        for key, end_date in watched_episodes(source_user, media_id).items()
        if key not in theirs
    )
    added = 0
    for (season, episode), end_date in missing:
        if mark_episode(target_user, media_id, season, episode, end_date):
            added += 1
    return added


def mark_movie(user, media_id, end_date=None):
    """Mark a movie as watched for a user. Returns False if already watched."""
    end_date = end_date or timezone.now().replace(second=0, microsecond=0)
    item = Item.objects.filter(
        media_id=media_id,
        source=Sources.TMDB.value,
        media_type=MediaTypes.MOVIE.value,
    ).first()
    if item is None:
        metadata = services.get_media_metadata(
            MediaTypes.MOVIE.value,
            media_id,
            Sources.TMDB.value,
        )
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=metadata["title"],
            image=metadata["image"],
        )

    latest = Movie.objects.filter(user=user, item=item).order_by("-created_at").first()
    if latest is not None and latest.status == Status.COMPLETED.value:
        return False
    if latest is not None:
        latest.status = Status.COMPLETED.value
        latest.end_date = end_date
        latest.save()
    else:
        Movie.objects.create(
            user=user,
            item=item,
            status=Status.COMPLETED.value,
            end_date=end_date,
        )
    logger.info("Marked %s as watched for %s", item, user)
    return True


def unmark_movie(user, media_id):
    """Remove the latest completed entry of a movie for a user."""
    movie = (
        Movie.objects.filter(
            user=user,
            item__media_id=media_id,
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.MOVIE.value,
            status=Status.COMPLETED.value,
        )
        .order_by("-created_at")
        .first()
    )
    if movie is None:
        return False
    movie.delete()
    logger.info("Removed %s for %s", movie.item, user)
    return True


def movie_end_date(user, media_id):
    """Return when a user watched a movie, or None."""
    return (
        Movie.objects.filter(
            user=user,
            item__media_id=media_id,
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.MOVIE.value,
            status=Status.COMPLETED.value,
        )
        .order_by("-created_at")
        .values_list("end_date", flat=True)
        .first()
    )


# Panel on the media pages


@dataclass
class PanelRow:
    """A member and what they watched of the media."""

    member: Member
    is_viewer: bool
    allowed: bool
    watched: bool = False
    latest: tuple | None = None
    count: int = 0
    missing: int = 0
    episodes: dict = field(default_factory=dict)


@dataclass
class EpisodeRow:
    """An episode and which members watched it."""

    number: int
    title: str
    chips: list = field(default_factory=list)


def _episode_titles(media_id, season_number):
    try:
        season = tmdb.tv_with_seasons(media_id, [season_number])[
            f"season/{season_number}"
        ]
    except Exception:
        logger.exception(
            "Could not fetch the episodes of %s S%s",
            media_id,
            season_number,
        )
        return {}
    return {
        episode["episode_number"]: episode.get("title") or episode.get("name") or ""
        for episode in season.get("episodes", [])
    }


def panel(viewer, media_type, media_id, season_number=None):
    """Return the context of the household panel, or None to show nothing."""
    household = members()
    if len(household) < MIN_MEMBERS or media_type not in (
        *ratings.RATED_TYPES,
        "season",
    ):
        return None

    rated_type = MediaTypes.TV.value if media_type == "season" else media_type
    rating = ratings.get_rating(rated_type, media_id)
    age = rating.age if rating else None
    viewer_limit = next((m.age_limit for m in household if m.id == viewer.id), None)
    if not is_allowed(viewer_limit, age, strict=True):
        return None

    rows = []
    mine = {}
    if media_type != MediaTypes.MOVIE.value:
        mine = watched_episodes(viewer, media_id)
    for member in household:
        row = PanelRow(
            member=member,
            is_viewer=member.id == viewer.id,
            allowed=is_allowed(member.age_limit, age, strict=False),
        )
        if media_type == MediaTypes.MOVIE.value:
            row.watched = movie_end_date(member.user, media_id) is not None
        else:
            theirs = mine if row.is_viewer else watched_episodes(member.user, media_id)
            row.count = len(theirs)
            row.latest = max(theirs) if theirs else None
            row.missing = len(set(mine) - set(theirs))
            row.episodes = theirs
        rows.append(row)

    episodes = []
    if media_type == "season":
        titles = _episode_titles(media_id, season_number)
        numbers = sorted(
            {
                episode
                for row in rows
                for (season, episode) in row.episodes
                if season == season_number
            },
        )
        episodes = [
            EpisodeRow(
                number=number,
                title=titles.get(number, ""),
                chips=[(row, (season_number, number) in row.episodes) for row in rows],
            )
            for number in numbers
        ]

    return {
        "media_type": media_type,
        "media_id": media_id,
        "season_number": season_number,
        "rating": rating,
        "rows": rows,
        "episodes": episodes,
    }


# Household page


@dataclass
class Activity:
    """Something a member watched recently."""

    item: Item
    when: datetime.datetime
    season: int | None = None
    episode: int | None = None
    count: int = 1


def _recent_activity(user, since):
    """Return the latest shows and movies a user watched since a date."""
    latest = {}
    episodes = (
        Episode.objects.filter(related_season__user=user, end_date__gte=since)
        .select_related("related_season__related_tv__item", "item")
        .order_by("end_date")
    )
    for episode in episodes:
        tv_item = episode.related_season.related_tv.item
        previous = latest.get(tv_item.id)
        latest[tv_item.id] = Activity(
            item=tv_item,
            when=episode.end_date,
            season=episode.item.season_number,
            episode=episode.item.episode_number,
            count=(previous.count + 1) if previous else 1,
        )
    movies = Movie.objects.filter(
        user=user,
        status=Status.COMPLETED.value,
        end_date__gte=since,
    ).select_related("item")
    activity = list(latest.values())
    activity.extend(Activity(item=movie.item, when=movie.end_date) for movie in movies)
    activity.sort(key=lambda entry: entry.when, reverse=True)
    return activity[:ACTIVITY_LIMIT]


@dataclass
class SharedShow:
    """A show several members follow, with where each one is."""

    item: Item
    progress: list
    ahead: Member | None = None


def _shared_shows(household):
    """Return the shows followed by at least two members."""
    followers = defaultdict(list)
    tvs = TV.objects.filter(
        user__in=[member.user for member in household],
        item__source=Sources.TMDB.value,
        status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value],
    ).select_related("item")
    for tv in tvs:
        followers[tv.item_id].append(tv)

    shared_items = {
        item_id: tv_list[0].item
        for item_id, tv_list in followers.items()
        if len(tv_list) > 1
    }
    if not shared_items:
        return []

    by_member = {member.id: member for member in household}
    watched = defaultdict(lambda: defaultdict(set))
    episodes = Episode.objects.filter(
        related_season__related_tv__item_id__in=shared_items,
        related_season__user__in=[member.user for member in household],
    ).values_list(
        "related_season__related_tv__item_id",
        "related_season__user_id",
        "item__season_number",
        "item__episode_number",
    )
    for item_id, user_id, season, episode in episodes:
        watched[item_id][user_id].add((season, episode))

    shows = []
    for item_id, item in shared_items.items():
        follower_ids = [tv.user_id for tv in followers[item_id]]
        progress = []
        for user_id in follower_ids:
            seen = watched[item_id][user_id]
            progress.append(
                {
                    "member": by_member[user_id],
                    "latest": max(seen) if seen else None,
                    "seen": seen,
                },
            )
        union = set().union(*(entry["seen"] for entry in progress))
        for entry in progress:
            entry["behind"] = len(union - entry["seen"])
        show = SharedShow(item=item, progress=progress)
        leaders = [entry for entry in progress if entry["behind"] == 0]
        if len(leaders) == 1 and any(entry["behind"] for entry in progress):
            show.ahead = leaders[0]["member"]
        shows.append(show)

    shows.sort(key=lambda show: (show.ahead is None, str(show.item.title).lower()))
    return shows


def overview(viewer):
    """Return the context of the household page."""
    household = members()
    viewer_limit = next((m.age_limit for m in household if m.id == viewer.id), None)
    since = timezone.now() - datetime.timedelta(days=ACTIVITY_DAYS)

    activity = {member.id: _recent_activity(member.user, since) for member in household}
    shared = _shared_shows(household)

    items = [entry.item for entries in activity.values() for entry in entries]
    items += [show.item for show in shared]
    ages = ratings.rating_map((item.media_type, item.media_id) for item in items)
    missing = [
        item
        for item in items
        if (item.media_type, str(item.media_id)) not in ages
        and item.media_type in ratings.RATED_TYPES
    ]

    def visible(item):
        age = ages.get((item.media_type, str(item.media_id)))
        return is_allowed(viewer_limit, age, strict=True)

    return {
        "members": household,
        "activity": [
            (member, [entry for entry in activity[member.id] if visible(entry.item)])
            for member in household
        ],
        "shared": [show for show in shared if visible(show.item)],
        "missing_ratings": missing,
        "viewer_limit": viewer_limit,
    }
