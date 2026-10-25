from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from app.models import MediaTypes
from sosoblue import household, ratings, series, tasks


@require_GET
def waiting(request):
    """Show the series the user is up to date on, with what comes next."""
    shows = series.waiting_shows(request.user)
    context = {
        "dated": shows["dated"],
        "undated": shows["undated"],
        "unknown": shows["unknown"],
        "missing_status": series.missing_status_count(),
    }
    return render(request, "sosoblue/waiting.html", context)


@require_POST
def refresh_status(request):
    """Refresh the status of all tracked shows in the background."""
    tasks.refresh_series_status.delay()
    messages.info(
        request,
        _("Series statuses are being refreshed. Reload the page in a minute."),
    )
    return redirect("sosoblue:waiting")


@require_GET
def household_page(request):
    """Show what the household is watching."""
    context = household.overview(request.user)
    if context["missing_ratings"]:
        tasks.fill_content_ratings.delay(
            [item.id for item in context["missing_ratings"]],
        )
    context["age_limit_choices"] = household.AGE_LIMIT_CHOICES
    return render(request, "sosoblue/household.html", context)


@require_POST
def household_settings(request):
    """Save the age limit of each member (administrators only)."""
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    for member in household.members():
        value = request.POST.get(f"age_limit_{member.id}", "")
        age_limit = int(value) if value.isdigit() else None
        if age_limit is not None and age_limit not in household.AGE_LIMIT_CHOICES:
            return HttpResponseBadRequest()
        household.set_age_limit(member.user, age_limit)
    messages.success(request, _("Household settings saved."))
    return redirect("sosoblue:household")


def _panel_params(data):
    media_type = data.get("media_type", "")
    media_id = data.get("media_id", "")
    season = data.get("season_number", "")
    if not media_id.isdigit() or media_type not in (*ratings.RATED_TYPES, "season"):
        return None
    if media_type == "season":
        if not season.isdigit():
            return None
        return media_type, media_id, int(season)
    return media_type, media_id, None


def _render_panel(request, media_type, media_id, season_number):
    context = household.panel(request.user, media_type, media_id, season_number)
    if context is None:
        return HttpResponse("")
    return render(request, "sosoblue/household_panel.html", context)


@require_GET
def household_panel(request):
    """Return the household panel of a movie, show or season page."""
    params = _panel_params(request.GET)
    if params is None:
        return HttpResponseBadRequest()
    return _render_panel(request, *params)


def _target(request, media_type, media_id):
    """Return the member to act on, if they may receive this media."""
    member = household.get_member(request.POST.get("member", ""))
    if member is None:
        return None
    rated_type = MediaTypes.TV.value if media_type == "season" else media_type
    rating = ratings.get_rating(rated_type, media_id)
    if not household.is_allowed(
        member.age_limit,
        rating.age if rating else None,
        strict=False,
    ):
        return None
    return member


@require_POST
def household_toggle(request):
    """Mark or unmark an episode or a movie for a member."""
    params = _panel_params(request.POST)
    if params is None:
        return HttpResponseBadRequest()
    media_type, media_id, season_number = params
    member = _target(request, media_type, media_id)
    if member is None:
        return HttpResponseForbidden()

    if media_type == MediaTypes.MOVIE.value:
        if not household.unmark_movie(member.user, media_id):
            end_date = household.movie_end_date(request.user, media_id)
            household.mark_movie(member.user, media_id, end_date)
    elif media_type == "season":
        episode = request.POST.get("episode", "")
        if not episode.isdigit():
            return HttpResponseBadRequest()
        episode = int(episode)
        if not household.unmark_episode(member.user, media_id, season_number, episode):
            mine = household.watched_episodes(request.user, media_id, season_number)
            household.mark_episode(
                member.user,
                media_id,
                season_number,
                episode,
                mine.get((season_number, episode)),
            )
    else:
        return HttpResponseBadRequest()
    return _render_panel(request, media_type, media_id, season_number)


@require_POST
def household_align(request):
    """Give a member every episode of the show the current user watched."""
    params = _panel_params(request.POST)
    if params is None or params[0] == MediaTypes.MOVIE.value:
        return HttpResponseBadRequest()
    media_type, media_id, season_number = params
    member = _target(request, media_type, media_id)
    if member is None or member.id == request.user.id:
        return HttpResponseForbidden()
    household.align_episodes(request.user, member.user, media_id)
    return _render_panel(request, media_type, media_id, season_number)
