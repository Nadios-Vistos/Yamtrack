from django.urls import path

from sosoblue import views

app_name = "sosoblue"

urlpatterns = [
    path("up-next/", views.waiting, name="waiting"),
    path("up-next/refresh/", views.refresh_status, name="refresh_status"),
    path("household/", views.household_page, name="household"),
    path("household/settings/", views.household_settings, name="household_settings"),
    path("household/panel/", views.household_panel, name="household_panel"),
    path("household/toggle/", views.household_toggle, name="household_toggle"),
    path("household/align/", views.household_align, name="household_align"),
]
