from django.conf import settings
from django.db import models


class League(models.Model):
    PLATFORM_ESPN = "espn"
    PLATFORM_SLEEPER = "sleeper"
    PLATFORMS = [
        (PLATFORM_ESPN, "ESPN"),
        (PLATFORM_SLEEPER, "Sleeper"),
    ]
    SYNC_IDLE = "idle"
    SYNC_QUEUED = "queued"
    SYNC_RUNNING = "running"
    SYNC_STATUSES = [
        (SYNC_IDLE, "Idle"),
        (SYNC_QUEUED, "Queued"),
        (SYNC_RUNNING, "Running"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="leagues",
    )
    platform = models.CharField(max_length=10, choices=PLATFORMS)
    league_id = models.CharField(max_length=100)
    name = models.CharField(max_length=255, blank=True)
    league_url = models.URLField(blank=True)
    # ESPN session cookies for private leagues. Stored as plaintext for this POC.
    espn_s2 = models.TextField(blank=True)
    swid = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sync_status = models.CharField(
        max_length=16,
        choices=SYNC_STATUSES,
        default=SYNC_IDLE,
    )
    sync_error = models.TextField(blank=True)

    class Meta:
        unique_together = ("user", "platform", "league_id")
        ordering = ["-created_at"]

    def __str__(self):
        label = self.name or self.league_id
        return f"{self.get_platform_display()}: {label}"

    @property
    def is_private_espn(self):
        return self.platform == self.PLATFORM_ESPN and bool(self.espn_s2 and self.swid)

    @property
    def is_syncing(self):
        return self.sync_status in {self.SYNC_QUEUED, self.SYNC_RUNNING}

    @property
    def last_synced(self):
        try:
            return self.snapshot.synced_at
        except LeagueSnapshot.DoesNotExist:
            return None


class LeagueSnapshot(models.Model):
    """Latest roster, activity, and matchup history pulled from ESPN or Sleeper."""

    league = models.OneToOneField(
        League,
        on_delete=models.CASCADE,
        related_name="snapshot",
    )
    synced_at = models.DateTimeField(auto_now=True)
    roster_data = models.JSONField(default=list)
    activity_data = models.JSONField(default=list)
    activity_error = models.TextField(blank=True)
    # [{week, matchups:[{matchup_id, home, away, winner, is_bye}]}]
    matchup_data = models.JSONField(default=list)
    # [{rank, team, wins, losses, ties, points_for, points_against}]
    standings_data = models.JSONField(default=list)

    def __str__(self):
        return f"Snapshot for {self.league}"
