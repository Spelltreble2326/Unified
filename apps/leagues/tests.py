from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.leagues.models import League
from apps.leagues.tasks import (
    QUEUE_FAILURE_MESSAGE,
    SyncEnqueueError,
    enqueue_league_sync,
    sync_league_task,
)


class SyncTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="owner",
            password="test-pass-123",
        )
        self.league = League.objects.create(
            user=self.user,
            platform=League.PLATFORM_SLEEPER,
            league_id="123",
            name="Home",
            sync_status=League.SYNC_QUEUED,
        )

    @patch("apps.leagues.services.sync_league")
    def test_task_marks_league_idle_after_success(self, sync_league):
        sync_league_task(self.league.pk)

        self.league.refresh_from_db()
        self.assertEqual(self.league.sync_status, League.SYNC_IDLE)
        self.assertEqual(self.league.sync_error, "")
        sync_league.assert_called_once()

    @patch("apps.leagues.services.sync_league", side_effect=RuntimeError("espn down"))
    def test_task_records_failure_and_returns_to_idle(self, sync_league):
        sync_league_task(self.league.pk)

        self.league.refresh_from_db()
        self.assertEqual(self.league.sync_status, League.SYNC_IDLE)
        self.assertEqual(self.league.sync_error, "espn down")
        sync_league.assert_called_once()

    @patch("apps.leagues.services.sync_league")
    def test_task_skips_when_sync_was_not_queued(self, sync_league):
        self.league.sync_status = League.SYNC_IDLE
        self.league.save(update_fields=["sync_status"])

        sync_league_task(self.league.pk)

        sync_league.assert_not_called()
        self.league.refresh_from_db()
        self.assertEqual(self.league.sync_status, League.SYNC_IDLE)


class EnqueueTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="owner",
            password="test-pass-123",
        )
        self.league = League.objects.create(
            user=self.user,
            platform=League.PLATFORM_SLEEPER,
            league_id="123",
            name="Home",
        )

    @patch("apps.leagues.tasks.sync_league_task.delay")
    def test_enqueue_marks_queued_and_publishes(self, delay):
        queued = enqueue_league_sync(self.league)

        self.assertTrue(queued)
        delay.assert_called_once_with(self.league.pk)
        self.league.refresh_from_db()
        self.assertEqual(self.league.sync_status, League.SYNC_QUEUED)
        self.assertEqual(self.league.sync_error, "")

    @patch("apps.leagues.tasks.sync_league_task.delay")
    def test_enqueue_does_not_publish_a_second_sync(self, delay):
        self.league.sync_status = League.SYNC_RUNNING
        self.league.save(update_fields=["sync_status"])

        queued = enqueue_league_sync(self.league)

        self.assertFalse(queued)
        delay.assert_not_called()

    @patch("apps.leagues.tasks.sync_league_task.delay", side_effect=ConnectionError("down"))
    def test_broker_failure_leaves_the_league_idle(self, delay):
        with self.assertRaises(SyncEnqueueError):
            enqueue_league_sync(self.league)

        delay.assert_called_once_with(self.league.pk)
        self.league.refresh_from_db()
        self.assertEqual(self.league.sync_status, League.SYNC_IDLE)
        self.assertEqual(self.league.sync_error, QUEUE_FAILURE_MESSAGE)


class SyncViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="owner",
            password="test-pass-123",
        )
        self.league = League.objects.create(
            user=self.user,
            platform=League.PLATFORM_ESPN,
            league_id="555",
            name="Work",
        )
        self.client.force_login(self.user)

    @patch("apps.leagues.tasks.sync_league_task.delay")
    def test_sync_button_queues_and_renders_syncing_state(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("league-sync", args=[self.league.pk]),
                HTTP_HX_REQUEST="true",
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Syncing")
        self.assertContains(response, "disabled")
        delay.assert_called_once_with(self.league.pk)
        self.league.refresh_from_db()
        self.assertEqual(self.league.sync_status, League.SYNC_QUEUED)

    def test_sync_status_refreshes_the_page_when_idle(self):
        response = self.client.get(
            reverse("league-sync-status", args=[self.league.pk]) + "?meta=1",
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response["HX-Refresh"], "true")
        self.assertContains(response, "Sync")

    def test_sync_status_keeps_polling_while_queued(self):
        self.league.sync_status = League.SYNC_QUEUED
        self.league.save(update_fields=["sync_status"])

        response = self.client.get(
            reverse("league-sync-status", args=[self.league.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertNotIn("HX-Refresh", response)
        self.assertContains(response, 'hx-trigger="every 2s"')
        self.assertContains(response, "Syncing")

    def test_sync_status_is_private_to_the_owner(self):
        other = get_user_model().objects.create_user(username="other", password="test-pass-123")
        self.client.force_login(other)

        response = self.client.get(reverse("league-sync-status", args=[self.league.pk]))

        self.assertEqual(response.status_code, 404)

    @patch("apps.leagues.forms.lookup_league", return_value="Remote Name")
    @patch("apps.leagues.tasks.sync_league_task.delay")
    def test_adding_a_league_queues_the_initial_sync(self, delay, lookup_league):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("league-add"),
                {
                    "platform": League.PLATFORM_SLEEPER,
                    "league_id": "999",
                    "name": "",
                    "league_url": "",
                    "espn_s2": "",
                    "swid": "",
                },
            )

        self.assertRedirects(response, reverse("league-list"))
        league = League.objects.get(league_id="999")
        self.assertEqual(league.name, "Remote Name")
        self.assertEqual(league.sync_status, League.SYNC_QUEUED)
        delay.assert_called_once_with(league.pk)
        lookup_league.assert_called_once()
