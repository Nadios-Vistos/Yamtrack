"""Read the viewing history of a Plex Media Server account."""

import datetime
import logging

import requests
from django.utils import timezone

from sosoblue.history.parsers import Entry

logger = logging.getLogger(__name__)

TIMEOUT = 30
PAGE_SIZE = 500


class PlexError(Exception):
    """The Plex server could not be reached or refused the request."""


class PlexClient:
    """Minimal client for the Plex Media Server API."""

    def __init__(self, url, token):
        """Store the server address and token."""
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"X-Plex-Token": token, "Accept": "application/json"},
        )
        self.metadata_cache = {}

    def get(self, path, **params):
        """Return the MediaContainer of a Plex API path."""
        try:
            response = self.session.get(
                f"{self.url}{path}",
                params=params,
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise PlexError(str(error)) from error
        if response.status_code == requests.codes.unauthorized:
            message = "unauthorized"
            raise PlexError(message)
        if response.status_code == requests.codes.not_found:
            return None
        if not response.ok:
            message = f"HTTP {response.status_code}"
            raise PlexError(message)
        try:
            return response.json().get("MediaContainer", {})
        except ValueError as error:
            message = "invalid response"
            raise PlexError(message) from error

    def accounts(self):
        """Return [(id, name)] of the accounts known to the server."""
        container = self.get("/accounts") or {}
        return [
            (str(account["id"]), account.get("name") or "")
            for account in container.get("Account", [])
            if account.get("name")
        ]

    def history(self, account_id):
        """Return the history items of an account, oldest first."""
        items, start = [], 0
        while True:
            container = (
                self.get(
                    "/status/sessions/history/all",
                    accountID=account_id,
                    sort="viewedAt:asc",
                    **{
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": PAGE_SIZE,
                    },
                )
                or {}
            )
            page = container.get("Metadata", [])
            items.extend(page)
            start += len(page)
            total = container.get("totalSize", container.get("size", 0))
            if not page or start >= total:
                return items

    def tmdb_id(self, key):
        """Return the TMDB id of a library item (by its key), or ''."""
        if not key:
            return ""
        if key not in self.metadata_cache:
            tmdb_id = ""
            try:
                container = self.get(key, includeGuids=1) or {}
            except PlexError:
                container = {}
            for metadata in container.get("Metadata", [])[:1]:
                for guid in metadata.get("Guid", []):
                    if guid.get("id", "").startswith("tmdb://"):
                        tmdb_id = guid["id"].removeprefix("tmdb://")
            self.metadata_cache[key] = tmdb_id
        return self.metadata_cache[key]


def plex_entries(client, account_id):
    """Return the history of a Plex account as entries."""
    entries = []
    for item in client.history(account_id):
        viewed_at = item.get("viewedAt")
        if not viewed_at:
            continue
        watched_at = datetime.datetime.fromtimestamp(int(viewed_at), tz=datetime.UTC)
        watched_at = timezone.localtime(watched_at)
        if item.get("type") == "episode":
            show = item.get("grandparentTitle") or ""
            entries.append(
                Entry(
                    kind="tv",
                    name=show,
                    season=item.get("parentIndex"),
                    episode_number=item.get("index"),
                    episode_title=item.get("title") or "",
                    watched_at=watched_at,
                    raw=f"{show} S{item.get('parentIndex')}E{item.get('index')}"
                    f" — {item.get('title') or ''}",
                    tmdb_id=client.tmdb_id(item.get("grandparentKey")),
                ),
            )
        elif item.get("type") == "movie":
            title = item.get("title") or ""
            entries.append(
                Entry(
                    kind="movie",
                    name=title,
                    year=item.get("year"),
                    watched_at=watched_at,
                    raw=f"{title} ({item.get('year')})" if item.get("year") else title,
                    tmdb_id=client.tmdb_id(item.get("key")),
                ),
            )
    return entries
