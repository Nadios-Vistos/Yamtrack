"""Read viewing histories into a common list of entries."""

import csv
import datetime
import io
import re
from collections import Counter
from dataclasses import dataclass

from django.utils import timezone

# Netflix separates the parts of a title with ": ". A colon preceded by a
# space (French typography, "Cursed : La rebelle") belongs to the name.
PART_SEPARATOR = re.compile(r"(?<! ): ")
SEASON_LABEL = re.compile(
    r"^(?P<show>.+?)(?<! ): (?P<season>(?:Saison|Partie|Volume|Livre|Chapitre"
    r"|Season|Part|Staffel|Temporada|Stagione)\b[^:]*|Mini-série|Série limitée"
    r"|Limited Series"
    r"|Collection[^:]*): (?P<episode>.+)$",
)
NUMBER_WORDS = {
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
}
PRIME_SEASON = re.compile(
    r"^(?P<show>.+?)\s*(?:[-:,\u2013]\s*)?(?:Saison|Season|Staffel|Temporada)\s*"
    r"(?P<number>\d+)\b.*$",
    re.IGNORECASE,
)
PRIME_EPISODE = re.compile(
    r"^(?:Épisode|Episode|Ép\.?|Ep\.?)\s*(?P<number>\d+)\s*[:.-]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)
# Trailers and extras that services list in the history
EXTRAS = re.compile(
    r"bande[- ]annonce|\bteaser\b|\btrailer\b|\(extrait\)|\bcoulisses\b"
    r"|aperçu de l'épisode|making[- ]of",
    re.IGNORECASE,
)
SHORT_YEAR = 100
MAX_MONTH = 12


class HistoryFormatError(ValueError):
    """The file is not the expected export."""


@dataclass
class Entry:
    """One viewing, before matching."""

    kind: str  # "tv", "movie" or "unknown"
    name: str  # show or movie title
    watched_at: datetime.datetime
    raw: str
    season: int | None = None
    episode_title: str = ""
    episode_number: int | None = None
    tmdb_id: str = ""
    year: int | None = None


def split_title(title):
    """Return (first part, rest) of a Netflix title, or (title, "")."""
    parts = PART_SEPARATOR.split(title, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (title, "")  # noqa: PLR2004


def _clean(text):
    return (text or "").replace("\xa0", " ").strip()


def _aware(value):
    return timezone.make_aware(value) if timezone.is_naive(value) else value


def season_number(label):
    """Return the season number of a label such as "Saison 2" or "Partie deux"."""
    label = _clean(label).lower()
    match = re.search(r"(\d+)", label)
    if match:
        return int(match.group(1))
    for word, number in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", label):
            return number
    if label in ("mini-série", "série limitée", "limited series"):
        return 1
    return None


def _read_csv(content):
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(content.lstrip("﻿"))))


def _netflix_date_order(dates):
    """Return "md" or "dm" depending on the date format of the export."""
    for value in dates:
        parts = value.split("/")
        if len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > MAX_MONTH:  # noqa: PLR2004
            return "dm"
    return "md"


def _netflix_date(value, order):
    first, second, year = (int(part) for part in value.strip().split("/"))
    if year < SHORT_YEAR:
        year += 2000
    month, day = (first, second) if order == "md" else (second, first)
    return _aware(datetime.datetime(year, month, day, 20, 0))  # noqa: DTZ001


def is_extra(title):
    """Return whether a history title is a trailer or a bonus."""
    return bool(EXTRAS.search(title))


def parse_netflix(content):
    """Read NetflixViewingHistory.csv (columns Title, Date)."""
    rows = _read_csv(content)
    if not rows or not {"Title", "Date"} <= set(rows[0]):
        columns = "Title,Date"
        raise HistoryFormatError(columns)
    order = _netflix_date_order(row["Date"] for row in rows)

    entries, others = [], []
    for row in rows:
        title = _clean(row["Title"])
        if not title or title.startswith(":") or is_extra(title):
            continue
        watched_at = _netflix_date(row["Date"], order)
        match = SEASON_LABEL.match(title)
        if match:
            entries.append(
                Entry(
                    kind="tv",
                    name=match["show"].strip(),
                    season=season_number(match["season"]),
                    episode_title=match["episode"].strip(),
                    watched_at=watched_at,
                    raw=title,
                ),
            )
        else:
            others.append((title, watched_at))

    # "Show: Episode" without a season: a show if the prefix comes back
    prefixes = Counter(
        split_title(title)[0] for title, _ in others if split_title(title)[1]
    )
    for title, watched_at in others:
        show, episode = split_title(title)
        if episode and prefixes[show] > 1:
            entries.append(
                Entry(
                    kind="tv",
                    name=show.strip(),
                    episode_title=episode.strip(),
                    watched_at=watched_at,
                    raw=title,
                ),
            )
        else:
            entries.append(
                Entry(kind="unknown", name=title, watched_at=watched_at, raw=title),
            )
    return entries


def parse_prime(content):
    """Read the Prime Video export (Date Watched, Type, Title, Episode Title)."""
    rows = _read_csv(content)
    needed = {"Date Watched", "Type", "Title", "Episode Title"}
    if not rows or not needed <= set(rows[0]):
        raise HistoryFormatError(", ".join(sorted(needed)))

    entries = []
    for row in rows:
        title = _clean(row["Title"])
        if not title or is_extra(f"{title} {row['Episode Title']}"):
            continue
        watched_at = _aware(
            datetime.datetime.fromisoformat(row["Date Watched"].strip()[:19]),
        )
        if row["Type"].strip().lower() == "movie":
            entries.append(
                Entry(kind="movie", name=title, watched_at=watched_at, raw=title),
            )
            continue
        match = PRIME_SEASON.match(title)
        show = match["show"].strip(" -:,\u2013") if match else title
        episode_raw = _clean(row["Episode Title"])
        episode = PRIME_EPISODE.match(episode_raw)
        entries.append(
            Entry(
                kind="tv",
                name=show,
                season=int(match["number"]) if match else None,
                episode_number=int(episode["number"]) if episode else None,
                episode_title=(episode["title"] if episode else episode_raw)
                or episode_raw,
                watched_at=watched_at,
                raw=f"{title} — {episode_raw}",
            ),
        )
    return entries
