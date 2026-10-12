from django.urls import path

from apps.leagues import views

urlpatterns = [
    path("", views.LeagueListView.as_view(), name="league-list"),
    path("add/", views.LeagueCreateView.as_view(), name="league-add"),
    path("fields/", views.league_fields_partial, name="league-fields"),
    path("validate/", views.validate_league, name="league-validate"),
    path("extension/", views.download_extension, name="extension-download"),
    path("<int:pk>/sync/", views.league_sync, name="league-sync"),
    path("<int:pk>/sync/status/", views.league_sync_status, name="league-sync-status"),
    path("<int:pk>/standings/", views.league_standings, name="league-standings"),
    path("<int:pk>/roster/", views.league_roster, name="league-roster"),
    path("<int:pk>/activity/", views.league_activity, name="league-activity"),
    path("<int:pk>/matchups/", views.league_matchups, name="league-matchups"),
    path(
        "<int:pk>/matchups/<int:week>/",
        views.league_matchup_detail,
        name="league-matchup-detail",
    ),
    path("<int:pk>/delete/", views.league_delete, name="league-delete"),
]
