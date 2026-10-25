from django.urls import path

from sosoblue import views, views_history

app_name = "sosoblue"

urlpatterns = [
    path("up-next/", views.waiting, name="waiting"),
    path("up-next/refresh/", views.refresh_status, name="refresh_status"),
    path("household/", views.household_page, name="household"),
    path("household/settings/", views.household_settings, name="household_settings"),
    path("household/panel/", views.household_panel, name="household_panel"),
    path("household/toggle/", views.household_toggle, name="household_toggle"),
    path("household/align/", views.household_align, name="household_align"),
    path("history/", views_history.history_list, name="history_list"),
    path("history/start/", views_history.history_start, name="history_start"),
    path("history/plex/", views_history.plex_accounts, name="plex_accounts"),
    path(
        "history/<int:import_id>/", views_history.history_detail, name="history_detail"
    ),
    path(
        "history/<int:import_id>/apply/",
        views_history.history_apply,
        name="history_apply",
    ),
    path(
        "history/<int:import_id>/delete/",
        views_history.history_delete,
        name="history_delete",
    ),
    path(
        "history/<int:import_id>/group/<int:group_id>/toggle/",
        views_history.group_toggle,
        name="history_group_toggle",
    ),
    path(
        "history/<int:import_id>/group/<int:group_id>/fix/",
        views_history.group_fix,
        name="history_group_fix",
    ),
]
