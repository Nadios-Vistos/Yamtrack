"""Match a viewing history with TMDB, then import what was reviewed."""

import logging
from collections import defaultdict

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from app.models import TV, Episode, MediaTypes, Movie, Season, Sources, Status
from app.providers import tmdb
from sosoblue import household, ratings
from sosoblue.history import parsers
from sosoblue.history.matching import (
    TmdbClient,
    find_movie,
    find_show,
    match_episode,
    norm,
)
from sosoblue.history.plex import PlexClient, plex_entries
from sosoblue.models import HistoryImportStatus, HistorySource, ImportGroup, ImportLine

logger = logging.getLogger(__name__)


def read_entries(history_import):
    """Return the entries of an import, from its file or its Plex server."""
    if history_import.source == HistorySource.NETFLIX:
        return parsers.parse_netflix(history_import.raw_data)
    if history_import.source == HistorySource.PRIME:
        return parsers.parse_prime(history_import.raw_data)
    client = PlexClient(history_import.plex_url, history_import.plex_token)
    try:
        return plex_entries(client, history_import.plex_account)
    finally:
        # The token is only needed to read the history once.
        history_import.plex_token = ""
        history_import.save(update_fields=["plex_token"])


def group_entries(entries):
    """Group entries by show or movie: {(kind, name, tmdb_id): [entries]}."""
    groups = defaultdict(list)
    for entry in entries:
        key = (entry.kind, entry.tmdb_id or norm(entry.name), entry.tmdb_id)
        groups[key].append(entry)
    return groups


def _set_match(group, media_type, result):
    group.media_type = media_type
    group.media_id = str(result["id"])
    group.media_title = result.get("name") or result.get("title") or ""
    group.media_image = tmdb.get_image_url(result.get("poster_path"))


def _is_exact(result, name, *keys):
    return bool(result) and norm(name) in {norm(result.get(key)) for key in keys}


def _resolve_show(client, entries, watched_on):
    first = entries[0]
    titles = [entry.episode_title for entry in entries if entry.episode_title]
    show = find_show(client, first.name, titles)
    if show:
        return MediaTypes.TV.value, show
    # "Movie - Part 1: Subtitle" looks like a show but is a movie
    movie = find_movie(client, first.raw, watched_on)
    if _is_exact(movie, first.raw, "title", "original_title"):
        return MediaTypes.MOVIE.value, movie
    return MediaTypes.TV.value, None


def _resolve_unknown(client, name, watched_on):
    """Resolve a Netflix title without a season: a movie or a show watched once."""
    movie = find_movie(client, name, watched_on)
    if _is_exact(movie, name, "title", "original_title"):
        return MediaTypes.MOVIE.value, movie
    candidates = [name]
    prefix, rest = parsers.split_title(name)
    if rest:
        candidates.append(prefix)
    for candidate in candidates:
        show = find_show(client, candidate)
        if _is_exact(show, candidate, "name", "original_name"):
            return MediaTypes.TV.value, show
    return MediaTypes.MOVIE.value, movie


def resolve_group(client, kind, entries):
    """Return (media_type, TMDB result) for a group of entries.

    The media type may differ from the guessed kind: a Netflix title with a
    colon can be a movie, a show watched once, or a movie in several parts.
    """
    first = entries[0]
    if first.tmdb_id:
        if kind == MediaTypes.TV.value:
            result = client.show(first.tmdb_id)
        else:
            result = client.movie(first.tmdb_id)
        if result.get("id"):
            return kind, result

    watched_on = min(entry.watched_at for entry in entries).date().isoformat()
    if kind == MediaTypes.TV.value:
        return _resolve_show(client, entries, watched_on)
    if kind == MediaTypes.MOVIE.value:
        return kind, find_movie(client, first.name, watched_on, first.year)
    return _resolve_unknown(client, first.name, watched_on)


def match_lines(client, group):
    """Find the season and episode of each line of a show group."""
    lines = list(group.lines.exclude(raw__startswith="+ "))
    group.lines.filter(raw__startswith="+ ").delete()
    counts = client.season_counts(group.media_id) if group.media_id else {}
    seasons = sorted(number for number in counts if number > 0)
    if 0 in counts:
        seasons.append(0)
    titles_by_season = defaultdict(set)

    for line in lines:
        found = None
        if group.media_id:
            if (
                line.season is not None
                and line.episode_hint
                and line.episode_hint <= counts.get(line.season, 0)
            ):
                found = (line.season, line.episode_hint)
            else:
                found = match_episode(
                    client,
                    group.media_id,
                    seasons,
                    line.episode_title,
                    line.season,
                )
        if line.season is not None and line.episode_title:
            titles_by_season[line.season].add(norm(line.episode_title))
        line.matched_season, line.matched_episode = found or (None, None)
        line.state = ImportLine.MATCHED if found else ImportLine.UNMATCHED
    ImportLine.objects.bulk_update(
        lines,
        ["matched_season", "matched_episode", "state"],
    )

    _cover_complete_seasons(group, lines, counts, titles_by_season)


def _cover_complete_seasons(group, lines, counts, titles_by_season):
    """Fill in a season whose every episode title appears in the history.

    Such a season was watched entirely even when some titles did not match
    (translations differ between services).
    """
    unmatched = defaultdict(list)
    for line in lines:
        if line.state == ImportLine.UNMATCHED and line.season is not None:
            unmatched[line.season].append(line)
    for season, season_lines in unmatched.items():
        count = counts.get(season, 0)
        if not count or len(titles_by_season[season]) < count:
            continue
        matched = {
            line.matched_episode
            for line in lines
            if line.state == ImportLine.MATCHED and line.matched_season == season
        }
        last = max(line.watched_at for line in season_lines)
        ImportLine.objects.bulk_create(
            ImportLine(
                group=group,
                raw=f"+ S{season}E{episode}",
                season=season,
                watched_at=last,
                matched_season=season,
                matched_episode=episode,
                state=ImportLine.MATCHED,
            )
            for episode in range(1, count + 1)
            if episode not in matched
        )
        for line in season_lines:
            line.state = ImportLine.COVERED
        ImportLine.objects.bulk_update(season_lines, ["state"])


def _split_episode_titles(group):
    """Use the part after "Show: " as the episode title of each line."""
    lines = list(group.lines.all())
    for line in lines:
        rest = parsers.split_title(line.raw)[1]
        if rest and not line.episode_title:
            line.episode_title = rest[:500]
    ImportLine.objects.bulk_update(lines, ["episode_title"])


def check_age(group, age_limit):
    """Store the age rating of a group and unselect it if above the limit."""
    group.age = None
    group.above_limit = False
    if not group.media_id:
        return
    rating = ratings.get_rating(group.media_type, group.media_id)
    group.age = rating.age if rating else None
    group.above_limit = not household.is_allowed(age_limit, group.age, strict=False)


def rematch_group(client, group, media_type, result, age_limit):
    """Apply a TMDB match to a group and match its lines again."""
    if result:
        _set_match(group, media_type, result)
    else:
        group.media_type = media_type
        group.media_id = group.media_title = group.media_image = ""
    check_age(group, age_limit)
    group.selected = bool(group.media_id) and not group.above_limit
    group.save()

    if group.media_type == MediaTypes.TV.value:
        match_lines(client, group)
    else:
        group.lines.filter(raw__startswith="+ ").delete()
        group.lines.update(
            state=ImportLine.MATCHED if group.media_id else ImportLine.UNMATCHED,
            matched_season=None,
            matched_episode=None,
        )


def match_import(history_import):
    """Read, group and match an import, then leave it for review."""
    try:
        entries = read_entries(history_import)
    except Exception as error:
        logger.exception("Could not read history import %s", history_import.id)
        history_import.status = HistoryImportStatus.FAILED
        history_import.error = str(error)
        history_import.save(update_fields=["status", "error"])
        return

    groups = group_entries(entries)
    history_import.groups.all().delete()
    history_import.total = len(groups)
    history_import.progress = 0
    history_import.raw_data = ""
    history_import.save(update_fields=["total", "progress", "raw_data"])

    client = TmdbClient()
    age_limit = household.age_limit_of(history_import.user)
    for (kind, _, _), group_list in sorted(groups.items(), key=lambda kv: kv[0][1]):
        group = ImportGroup.objects.create(
            history_import=history_import,
            kind=kind,
            name=group_list[0].name[:500],
        )
        ImportLine.objects.bulk_create(
            ImportLine(
                group=group,
                raw=entry.raw[:1000],
                season=entry.season,
                episode_title=entry.episode_title[:500],
                episode_hint=entry.episode_number,
                watched_at=entry.watched_at,
            )
            for entry in group_list
        )
        try:
            media_type, result = resolve_group(client, kind, group_list)
            if kind == "unknown" and media_type == MediaTypes.TV.value:
                _split_episode_titles(group)
            rematch_group(client, group, media_type, result, age_limit)
        except Exception:
            logger.exception("Could not match %s", group.name)
        history_import.progress += 1
        history_import.save(update_fields=["progress"])

    history_import.status = HistoryImportStatus.REVIEW
    history_import.save(update_fields=["status"])


def apply_import(history_import):
    """Import the selected groups for the user of the import."""
    user = history_import.user
    groups = list(
        history_import.groups.filter(selected=True)
        .exclude(media_id="")
        .prefetch_related("lines"),
    )
    history_import.total = sum(len(group.lines.all()) for group in groups)
    history_import.progress = 0
    history_import.save(update_fields=["total", "progress"])
    errors = []

    for group in groups:
        lines = [line for line in group.lines.all() if line.state == ImportLine.MATCHED]
        try:
            if group.media_type == MediaTypes.TV.value:
                for line in lines:
                    added = household.mark_episode(
                        user,
                        group.media_id,
                        line.matched_season,
                        line.matched_episode,
                        line.watched_at,
                    )
                    line.state = ImportLine.IMPORTED if added else ImportLine.EXISTING
            elif lines:
                added = household.mark_movie(
                    user,
                    group.media_id,
                    max(line.watched_at for line in lines),
                )
                for line in lines:
                    line.state = ImportLine.IMPORTED if added else ImportLine.EXISTING
        except Exception as error:
            logger.exception("Could not import %s", group)
            errors.append(f"{group.media_title or group.name}: {error}")
        with transaction.atomic():
            ImportLine.objects.bulk_update(lines, ["state"])
        history_import.progress += len(group.lines.all())
        history_import.save(update_fields=["progress"])

    history_import.status = HistoryImportStatus.DONE
    history_import.error = "\n".join(errors)
    history_import.finished_at = timezone.now()
    history_import.save(update_fields=["status", "error", "finished_at"])


def _remove_episode(user, media_id, line):
    """Delete the view of an episode that an import added."""
    episode = Episode.objects.filter(
        related_season__user=user,
        item__media_id=media_id,
        item__source=Sources.TMDB.value,
        item__media_type=MediaTypes.EPISODE.value,
        item__season_number=line.matched_season,
        item__episode_number=line.matched_episode,
        end_date=line.watched_at,
    ).first()
    if episode is not None:
        episode.delete()


def _remove_empty_show(user, media_id, since):
    """Delete the seasons and show an import created that are now empty."""
    Season.objects.filter(
        user=user,
        item__media_id=media_id,
        item__source=Sources.TMDB.value,
        created_at__gte=since,
    ).annotate(count=Count("episodes")).filter(count=0).delete()
    for tv in TV.objects.filter(
        user=user,
        item__media_id=media_id,
        item__source=Sources.TMDB.value,
        created_at__gte=since,
    ):
        if not Episode.objects.filter(related_season__related_tv=tv).exists():
            tv.delete()


def _remove_movie(user, media_id, since):
    """Delete the movie entry an import added, or put it back to planned."""
    movie = (
        Movie.objects.filter(
            user=user,
            item__media_id=media_id,
            item__source=Sources.TMDB.value,
            status=Status.COMPLETED.value,
        )
        .order_by("-created_at")
        .first()
    )
    if movie is None:
        return
    if movie.created_at >= since:
        movie.delete()
    else:
        movie.status = Status.PLANNING.value
        movie.end_date = None
        movie.save()


def undo_import(history_import):
    """Remove what an import added, and only that."""
    user = history_import.user
    since = history_import.created_at
    groups = list(
        history_import.groups.filter(lines__state=ImportLine.IMPORTED)
        .distinct()
        .prefetch_related("lines"),
    )
    history_import.total = len(groups)
    history_import.progress = 0
    history_import.save(update_fields=["total", "progress"])
    errors = []

    for group in groups:
        lines = [
            line for line in group.lines.all() if line.state == ImportLine.IMPORTED
        ]
        try:
            if group.media_type == MediaTypes.TV.value:
                for line in lines:
                    _remove_episode(user, group.media_id, line)
                _remove_empty_show(user, group.media_id, since)
            else:
                _remove_movie(user, group.media_id, since)
            for line in lines:
                line.state = ImportLine.MATCHED
            ImportLine.objects.bulk_update(lines, ["state"])
        except Exception as error:
            logger.exception("Could not undo %s", group)
            errors.append(f"{group.media_title or group.name}: {error}")
        history_import.progress += 1
        history_import.save(update_fields=["progress"])

    history_import.status = HistoryImportStatus.UNDONE
    history_import.error = "\n".join(errors)
    history_import.save(update_fields=["status", "error"])
