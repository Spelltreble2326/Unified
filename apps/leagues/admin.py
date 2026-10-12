from django.contrib import admin

from apps.leagues.models import League, LeagueSnapshot


@admin.register(League)
class LeagueAdmin(admin.ModelAdmin):
    list_display = ("name", "platform", "league_id", "user", "sync_status", "created_at")
    list_filter = ("platform",)
    search_fields = ("name", "league_id", "user__username")
    readonly_fields = ("created_at",)


@admin.register(LeagueSnapshot)
class LeagueSnapshotAdmin(admin.ModelAdmin):
    list_display = ("league", "synced_at")
    readonly_fields = ("synced_at",)
