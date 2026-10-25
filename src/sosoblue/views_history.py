"""Pages to import a Netflix, Prime Video or Plex viewing history."""

import re

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from app.models import MediaTypes
from sosoblue import household, tasks
from sosoblue.history import parsers
from sosoblue.history.matching import TmdbClient
from sosoblue.history.plex import PlexClient, PlexError
from sosoblue.history.runner import rematch_group
from sosoblue.models import (
    HistoryImport,
    HistoryImportStatus,
    HistorySource,
    ImportGroup,
    ImportLine,
)

MAX_FILE_SIZE = 20 * 1024 * 1024
TMDB_REFERENCE = re.compile(r"(?:themoviedb\.org/)?(?:(tv|movie)[/ ])?(\d+)")
PARSERS = {
    HistorySource.NETFLIX: parsers.parse_netflix,
    HistorySource.PRIME: parsers.parse_prime,
}


def _targets(user):
    """Return the members the user may import for."""
    if user.is_superuser:
        return household.members()
    return [member for member in household.members() if member.id == user.id]


def _get_import(request, import_id):
    history_import = get_object_or_404(HistoryImport, id=import_id)
    if not (
        request.user.is_superuser
        or request.user.id in (history_import.user_id, history_import.created_by_id)
    ):
        raise Http404
    return history_import


@require_GET
def history_list(request):
    """Show the import forms and the previous imports."""
    imports = HistoryImport.objects.select_related("user")
    if not request.user.is_superuser:
        imports = imports.filter(Q(user=request.user) | Q(created_by=request.user))
    return render(
        request,
        "sosoblue/history_list.html",
        {"imports": imports[:20], "targets": _targets(request.user)},
    )


@require_POST
def plex_accounts(request):
    """Return the accounts of a Plex server, to choose whose history to import."""
    url = request.POST.get("plex_url", "").strip()
    token = request.POST.get("plex_token", "").strip()
    accounts, error = [], ""
    if not url.startswith(("http://", "https://")) or not token:
        error = _("Enter the server address (starting with http://) and the token.")
    else:
        try:
            accounts = PlexClient(url, token).accounts()
        except PlexError as plex_error:
            error = _("Could not reach the Plex server: %(error)s") % {
                "error": plex_error,
            }
    return render(
        request,
        "sosoblue/plex_accounts.html",
        {"accounts": accounts, "error": error},
    )


@require_POST
def history_start(request):
    """Create an import and start matching it in the background."""
    source = request.POST.get("source", "")
    target = next(
        (m for m in _targets(request.user) if str(m.id) == request.POST.get("user")),
        None,
    )
    if source not in HistorySource.values or target is None:
        return HttpResponseBadRequest()

    history_import = HistoryImport(
        user=target.user,
        created_by=request.user,
        source=source,
    )
    if source == HistorySource.PLEX:
        history_import.plex_url = request.POST.get("plex_url", "").strip()
        history_import.plex_token = request.POST.get("plex_token", "").strip()
        history_import.plex_account = request.POST.get("plex_account", "").strip()
        history_import.file_name = request.POST.get("plex_account_name", "")[:255]
        if not all(
            (
                history_import.plex_url,
                history_import.plex_token,
                history_import.plex_account,
            ),
        ):
            messages.error(request, _("Choose the Plex account to import."))
            return redirect("sosoblue:history_list")
    else:
        upload = request.FILES.get("file")
        if upload is None or upload.size > MAX_FILE_SIZE:
            messages.error(request, _("Choose the exported file (20 MB at most)."))
            return redirect("sosoblue:history_list")
        try:
            content = upload.read().decode("utf-8-sig")
            if not PARSERS[source](content):
                raise parsers.HistoryFormatError
        except (UnicodeDecodeError, ValueError, KeyError):
            messages.error(
                request,
                _("This file does not look like a %(source)s export.")
                % {"source": HistorySource(source).label},
            )
            return redirect("sosoblue:history_list")
        history_import.raw_data = content
        history_import.file_name = upload.name[:255]

    history_import.save()
    tasks.match_history_import.delay(history_import.id)
    return redirect("sosoblue:history_detail", import_id=history_import.id)


def _groups(history_import):
    groups = history_import.groups.prefetch_related("lines").annotate(
        matched_count=Count("lines", filter=Q(lines__state=ImportLine.MATCHED)),
        unmatched_count=Count("lines", filter=Q(lines__state=ImportLine.UNMATCHED)),
        imported_count=Count("lines", filter=Q(lines__state=ImportLine.IMPORTED)),
        existing_count=Count("lines", filter=Q(lines__state=ImportLine.EXISTING)),
        line_count=Count("lines", filter=~Q(lines__raw__startswith="+ ")),
    )
    return sorted(
        groups,
        key=lambda group: (
            not (not group.media_id or group.unmatched_count or group.above_limit),
            group.name.lower(),
        ),
    )


def _group_context(group):
    return (
        ImportGroup.objects.filter(id=group.id)
        .annotate(
            matched_count=Count("lines", filter=Q(lines__state=ImportLine.MATCHED)),
            unmatched_count=Count(
                "lines",
                filter=Q(lines__state=ImportLine.UNMATCHED),
            ),
            imported_count=Count("lines", filter=Q(lines__state=ImportLine.IMPORTED)),
            existing_count=Count("lines", filter=Q(lines__state=ImportLine.EXISTING)),
            line_count=Count("lines", filter=~Q(lines__raw__startswith="+ ")),
        )
        .get()
    )


@require_GET
def history_detail(request, import_id):
    """Show the progress, the review screen or the result of an import."""
    history_import = _get_import(request, import_id)
    running = history_import.status in (
        HistoryImportStatus.MATCHING,
        HistoryImportStatus.IMPORTING,
    )
    if request.headers.get("HX-Request") and request.GET.get("progress"):
        if not running:
            response = HttpResponse("")
            response["HX-Refresh"] = "true"
            return response
        return render(
            request,
            "sosoblue/history_progress.html",
            {"history_import": history_import, "Status": HistoryImportStatus},
        )

    context = {
        "history_import": history_import,
        "Status": HistoryImportStatus,
        "review": history_import.status == HistoryImportStatus.REVIEW,
    }
    if history_import.status in (HistoryImportStatus.REVIEW, HistoryImportStatus.DONE):
        groups = _groups(history_import)
        skipped = 0
        if history_import.status == HistoryImportStatus.DONE:
            kept = [g for g in groups if g.imported_count or g.existing_count]
            skipped = len(groups) - len(kept)
            groups = kept
        context.update(
            {
                "groups": groups,
                "skipped_count": skipped,
                "selected_count": sum(
                    group.matched_count for group in groups if group.selected
                ),
                "imported_count": sum(group.imported_count for group in groups),
                "existing_count": sum(group.existing_count for group in groups),
            },
        )
    return render(request, "sosoblue/history_detail.html", context)


def _review_group(request, import_id, group_id):
    history_import = _get_import(request, import_id)
    if history_import.status != HistoryImportStatus.REVIEW:
        raise Http404
    return get_object_or_404(ImportGroup, id=group_id, history_import=history_import)


@require_POST
def group_toggle(request, import_id, group_id):
    """Select or unselect a group for the import."""
    group = _review_group(request, import_id, group_id)
    if group.media_id:
        group.selected = not group.selected
        group.save(update_fields=["selected"])
    return render(
        request,
        "sosoblue/history_group.html",
        {"group": _group_context(group), "review": True},
    )


@require_POST
def group_fix(request, import_id, group_id):
    """Set the TMDB show or movie of a group from a TMDB link or id."""
    group = _review_group(request, import_id, group_id)
    match = TMDB_REFERENCE.search(request.POST.get("reference", ""))
    error = ""
    if match is None:
        error = _("Paste a TMDB link, for example https://www.themoviedb.org/tv/1399")
    else:
        media_type = match.group(1) or group.media_type or MediaTypes.TV.value
        client = TmdbClient()
        result = (
            client.show(match.group(2))
            if media_type == MediaTypes.TV.value
            else client.movie(match.group(2))
        )
        if not result.get("id"):
            error = _("Nothing found on TMDB with this link.")
        else:
            rematch_group(
                client,
                group,
                media_type,
                result,
                household.age_limit_of(group.history_import.user),
            )
    return render(
        request,
        "sosoblue/history_group.html",
        {"group": _group_context(group), "review": True, "error": error},
    )


@require_POST
def history_apply(request, import_id):
    """Import the selected groups in the background."""
    history_import = _get_import(request, import_id)
    if history_import.status != HistoryImportStatus.REVIEW:
        return HttpResponseBadRequest()
    history_import.status = HistoryImportStatus.IMPORTING
    history_import.save(update_fields=["status"])
    tasks.apply_history_import.delay(history_import.id)
    return redirect("sosoblue:history_detail", import_id=history_import.id)


@require_POST
def history_delete(request, import_id):
    """Delete an import (what was already imported stays)."""
    history_import = _get_import(request, import_id)
    if history_import.status in (
        HistoryImportStatus.MATCHING,
        HistoryImportStatus.IMPORTING,
    ):
        return HttpResponseBadRequest()
    history_import.delete()
    return redirect("sosoblue:history_list")
