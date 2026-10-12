import logging

from celery import shared_task
from django.db import transaction

from apps.leagues.models import League

logger = logging.getLogger(__name__)

QUEUE_FAILURE_MESSAGE = (
    "Could not queue the sync. Start Redis and the Celery worker, then try again."
)


class SyncEnqueueError(Exception):
    """The sync could not be handed to the worker."""


def enqueue_league_sync(league):
    """Mark a league as queued and publish the sync task after commit.

    Returns False when a sync is already queued or running. Raises
    SyncEnqueueError when the broker rejects the task; the league is left
    idle with ``sync_error`` set so the user can retry.
    """
    publish_errors = []
    with transaction.atomic():
        updated = League.objects.filter(
            pk=league.pk,
            sync_status=League.SYNC_IDLE,
        ).update(sync_status=League.SYNC_QUEUED, sync_error="")
        if not updated:
            return False
        league_id = league.pk
        transaction.on_commit(lambda: _publish_sync(league_id, publish_errors))
    if publish_errors:
        raise SyncEnqueueError(publish_errors[0])
    return True


def _publish_sync(league_id, publish_errors):
    try:
        sync_league_task.delay(league_id)
    except Exception as exc:
        logger.exception("Failed to queue sync for league %s", league_id)
        League.objects.filter(
            pk=league_id,
            sync_status=League.SYNC_QUEUED,
        ).update(sync_status=League.SYNC_IDLE, sync_error=QUEUE_FAILURE_MESSAGE)
        publish_errors.append(exc)


@shared_task(ignore_result=True, acks_late=True)
def sync_league_task(league_id):
    """Pull ESPN or Sleeper data for one league outside the request cycle."""
    claimed = League.objects.filter(
        pk=league_id,
        sync_status__in=[League.SYNC_QUEUED, League.SYNC_RUNNING],
    ).update(sync_status=League.SYNC_RUNNING, sync_error="")
    if not claimed:
        return

    from apps.leagues.services import sync_league

    error = ""
    try:
        league = League.objects.get(pk=league_id)
        sync_league(league)
    except League.DoesNotExist:
        return
    except Exception as exc:
        logger.exception("Sync failed for league %s", league_id)
        error = _sync_error_message(exc)
    League.objects.filter(pk=league_id).update(
        sync_status=League.SYNC_IDLE,
        sync_error=error,
    )


def _sync_error_message(exc):
    text = str(exc).strip() or "Sync failed."
    if len(text) > 240:
        return f"{text[:237]}..."
    return text
