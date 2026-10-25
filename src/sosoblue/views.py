from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from sosoblue import series, tasks


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
