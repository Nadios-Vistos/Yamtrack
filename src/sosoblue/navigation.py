"""Order of the phone tab bar and of the media types in the menu."""

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import pgettext_lazy

from app.models import MediaTypes
from app.templatetags import app_tags
from sosoblue.models import NavigationSettings, default_tabs

MIN_TABS = 2
MAX_TABS = 5  # the "More" button comes on top

MEDIA_LABELS = {
    MediaTypes.TV.value: pgettext_lazy("tab bar", "Series"),
    MediaTypes.SEASON.value: pgettext_lazy("tab bar", "Seasons"),
    MediaTypes.MOVIE.value: pgettext_lazy("tab bar", "Movies"),
    MediaTypes.ANIME.value: pgettext_lazy("tab bar", "Anime"),
    MediaTypes.MANGA.value: pgettext_lazy("tab bar", "Manga"),
    MediaTypes.GAME.value: pgettext_lazy("tab bar", "Games"),
    MediaTypes.BOOK.value: pgettext_lazy("tab bar", "Books"),
    MediaTypes.COMIC.value: pgettext_lazy("tab bar", "Comics"),
    MediaTypes.BOARDGAME.value: pgettext_lazy("tab bar", "Board games"),
}

# Pages: key -> (label, icon template, url name)
PAGES = {
    "home": (pgettext_lazy("tab bar", "Home"), "app/icons/house.svg", "home"),
    "up_next": (
        pgettext_lazy("tab bar", "Up next"),
        "app/icons/alarm-clock.svg",
        "sosoblue:waiting",
    ),
    "household": (
        pgettext_lazy("tab bar", "Household"),
        "app/icons/persons.svg",
        "sosoblue:household",
    ),
    "calendar": (
        pgettext_lazy("tab bar", "Calendar"),
        "app/icons/calendar.svg",
        "calendar",
    ),
    "lists": (pgettext_lazy("tab bar", "Lists"), "app/icons/folder-plus.svg", "lists"),
    "statistics": (
        pgettext_lazy("tab bar", "Statistics"),
        "app/icons/bars-pyramid.svg",
        "statistics",
    ),
    "journal": (
        pgettext_lazy("tab bar", "Journal"),
        "app/icons/history.svg",
        "journal",
    ),
}


def _settings(user):
    cached = getattr(user, "_sosoblue_navigation", None)
    if cached is None:
        cached = NavigationSettings.objects.filter(user=user).first()
        if cached is None:
            cached = NavigationSettings(user=user)
        user._sosoblue_navigation = cached
    return cached


def ordered_media_types(user):
    """Return the user's enabled media types, in their chosen order."""
    enabled = user.get_enabled_media_types()
    saved = [media for media in _settings(user).media_order if media in enabled]
    return saved + [media for media in enabled if media not in saved]


def available_tabs(user):
    """Return every tab the user can put in the bar: {key: (label, svg)}.

    Media types follow the user's order and only the enabled ones are offered.
    """
    tabs = {"home": PAGES["home"]}
    for media_type in ordered_media_types(user):
        tabs[media_type] = (MEDIA_LABELS.get(media_type, media_type), None, None)
    tabs.update({key: page for key, page in PAGES.items() if key != "home"})
    return {
        key: (
            label,
            app_tags.icon(key, is_active=False, extra_classes="")
            if icon is None
            else mark_safe(render_to_string(icon, {"classes": ""})),  # noqa: S308
        )
        for key, (label, icon, _url) in tabs.items()
    }


def chosen_tabs(user):
    """Return the keys of the user's tabs, in order."""
    available = set(PAGES) | set(user.get_enabled_media_types())
    tabs = [key for key in _settings(user).tabs if key in available]
    return tabs if len(tabs) >= MIN_TABS else default_tabs()


def tab_url(user, key):
    """Return where a tab leads."""
    if key in PAGES:
        return reverse(PAGES[key][2])
    return reverse("medialist", kwargs={"username": user.username, "media_type": key})


def is_active(key, path, media_type):
    """Return whether a tab is the current page."""
    if key in PAGES:
        return path == reverse(PAGES[key][2])
    if key == MediaTypes.TV.value:
        return media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value)
    return media_type == key


def tab_bar(user, path, media_type):
    """Return the tabs of the phone tab bar."""
    available = available_tabs(user)
    return [
        {
            "key": key,
            "label": available[key][0],
            "svg": available[key][1],
            "url": tab_url(user, key),
            "active": is_active(key, path, media_type),
        }
        for key in chosen_tabs(user)
        if key in available
    ]


def save(user, tabs, media_order):
    """Store the tab bar and the media order. Returns whether it was valid."""
    available = set(PAGES) | set(user.get_enabled_media_types())
    tabs = list(dict.fromkeys(key for key in tabs if key in available))
    if not MIN_TABS <= len(tabs) <= MAX_TABS:
        return False
    enabled = user.get_enabled_media_types()
    settings = _settings(user)
    settings.tabs = tabs
    settings.media_order = list(
        dict.fromkeys(media for media in media_order if media in enabled),
    )
    settings.save()
    return True
