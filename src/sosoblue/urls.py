from django.urls import path

from sosoblue import views

app_name = "sosoblue"

urlpatterns = [
    path("up-next/", views.waiting, name="waiting"),
    path("up-next/refresh/", views.refresh_status, name="refresh_status"),
]
