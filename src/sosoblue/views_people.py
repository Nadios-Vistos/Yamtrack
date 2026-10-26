from django.http import Http404
from django.shortcuts import render
from django.utils.translation import pgettext
from django.views.decorators.http import require_GET

from app import helpers
from sosoblue import people


def _with_tracking(request, entries):
    """Add the user's tracking and the readable roles to each credit."""
    if not entries:
        return []
    enriched = helpers.enrich_items_with_user_data(
        request,
        [entry["item"] for entry in entries],
        "person",
    )
    tracked = {str(result["item"]["media_id"]): result["media"] for result in enriched}
    return [
        {
            **entry,
            "media": tracked.get(str(entry["item"]["media_id"])),
            "roles_label": people.roles_label(entry["roles"]),
        }
        for entry in entries
    ]


@require_GET
def person_page(request, person_id):
    """Show an actor or a director with their movies and TV shows."""
    person = people.person(person_id)
    if person is None:
        raise Http404
    shows = _with_tracking(request, person["shows"])
    movies = _with_tracking(request, person["movies"])
    context = {
        "person": person,
        "department": people.department_label(person["department"]),
        "shows": shows,
        "movies": movies,
        "sections": [
            (pgettext("person page section", "Series"), shows),
            (pgettext("person page section", "Movies"), movies),
        ],
        "tracked_count": sum(1 for entry in shows + movies if entry["media"]),
    }
    return render(request, "sosoblue/person.html", context)
