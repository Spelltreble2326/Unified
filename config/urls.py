from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("leagues/", include("apps.leagues.urls")),
    path("", RedirectView.as_view(pattern_name="league-list", permanent=False)),
]
