"""Strings used in upstream templates and translated in the sosoblue catalog.

makemessages only scans the sosoblue app, so the strings SosoBlue adds to
upstream templates are listed here to keep them in its catalog.
"""

from django.utils.translation import gettext_noop

STRINGS = [
    # templates/events/components/calendar_list.html
    gettext_noop("%(weekday)s, %(month_name)s %(day)s"),
    gettext_noop("%(number)s at %(time)s"),
    # templates/app/components/cast_card.html
    gettext_noop("Movies and series with %(name)s"),
]
