from django.apps import AppConfig


class LeaguesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.leagues"
    label = "leagues"

    def ready(self):
        from django.db.backends.signals import connection_created

        def _sqlite_pragmas(sender, connection, **kwargs):
            if connection.vendor != "sqlite":
                return
            if getattr(connection, "_wal_enabled", False):
                return
            connection._wal_enabled = True
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA busy_timeout=20000;")

        connection_created.connect(_sqlite_pragmas, dispatch_uid="leagues-sqlite-pragmas")
