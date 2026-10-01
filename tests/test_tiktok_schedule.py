import unittest
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import tiktok_schedule


class Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self.payload = payload or {}

    def json(self):
        return self.payload


class FakeLedger:
    def __init__(self, runs):
        self.runs = runs

    def _read(self):
        return {'schema': 1, 'runs': self.runs}, 'sha'


class TikTokScheduleTests(unittest.TestCase):
    def test_selects_three_baghdad_slots_once_and_catches_up_in_order(self):
        self.assertIsNone(tiktok_schedule.next_due_slot({}, datetime(2026, 10, 1, 2, 59, tzinfo=timezone.utc)))
        now = datetime(2026, 10, 1, 3, 4, tzinfo=timezone.utc)
        self.assertEqual(tiktok_schedule.next_due_slot({}, now), '2026-10-01/06:00')
        runs = {'1': {'schedule_slot': '2026-10-01/06:00', 'status': 'send_to_user_inbox'}}
        self.assertIsNone(tiktok_schedule.next_due_slot(runs, now))
        self.assertEqual(tiktok_schedule.next_due_slot(runs, datetime(2026, 10, 1, 9, 5, tzinfo=timezone.utc)),
                         '2026-10-01/12:00')
        runs['2'] = {'schedule_slot': '2026-10-01/12:00', 'status': 'send_to_user_inbox'}
        self.assertEqual(tiktok_schedule.next_due_slot(runs, datetime(2026, 10, 1, 15, 10, tzinfo=timezone.utc)),
                         '2026-10-01/18:00')

    def test_does_not_carry_missed_slots_into_a_new_day(self):
        runs = {'1': {'schedule_slot': '2026-09-30/18:00', 'status': 'send_to_user_inbox'}}
        self.assertEqual(tiktok_schedule.next_due_slot(runs, datetime(2026, 10, 1, 3, 1, tzinfo=timezone.utc)),
                         '2026-10-01/06:00')

    def test_pending_inbox_cap_stops_fifth_pending_share(self):
        now = datetime(2026, 10, 1, 3, tzinfo=timezone.utc)
        runs = {str(index): {'status': 'send_to_user_inbox', 'started_at': '2026-10-01T02:00:00Z'}
                for index in range(4)}
        session = Mock()
        session.get.return_value = Response(payload={'configured': True, 'connected': True,
                                                       'scope': 'video.upload'})
        env = {'DASHBOARD_URL': 'https://example.test', 'DASHBOARD_KEY': 'key'}
        self.assertEqual(tiktok_schedule.select_slot(now=now, ledger=FakeLedger(runs), env=env,
                                                      session=session), '2026-10-01/06:00')
        runs['4'] = {'status': 'send_to_user_inbox', 'started_at': '2026-10-01T02:00:00Z'}
        self.assertEqual(tiktok_schedule.pending_share_count(runs, now), 5)
        self.assertIsNone(tiktok_schedule.select_slot(now=now, ledger=FakeLedger(runs), env=env,
                                                       session=session))

    def test_tiktok_readiness_requires_connection_and_upload_scope(self):
        session = Mock()
        session.get.return_value = Response(payload={'configured': True, 'connected': True, 'scope': 'user.info.basic video.upload'})
        self.assertTrue(tiktok_schedule.tiktok_ready(session, 'https://example.test', 'dashboard-key'))
        session.get.return_value = Response(payload={'configured': True, 'connected': False, 'scope': ''})
        self.assertFalse(tiktok_schedule.tiktok_ready(session, 'https://example.test', 'dashboard-key'))
        session.get.return_value = Response(payload={'configured': True, 'connected': True, 'scope': 'user.info.basic'})
        self.assertFalse(tiktok_schedule.tiktok_ready(session, 'https://example.test', 'dashboard-key'))

    def test_unavailable_dashboard_fails_closed_without_raising(self):
        session = Mock()
        session.get.side_effect = tiktok_schedule.requests.RequestException('offline')
        self.assertFalse(tiktok_schedule.tiktok_ready(session, 'https://example.test', 'dashboard-key'))

    def test_global_automation_switch_disables_tiktok_schedule(self):
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / 'automation.json'
            config_path.write_text(json.dumps({'enabled': False}), encoding='utf-8')
            session = Mock()
            env = {'DASHBOARD_URL': 'https://example.test', 'DASHBOARD_KEY': 'key'}
            self.assertIsNone(tiktok_schedule.select_slot(
                env=env, session=session, automation_path=config_path))
            session.get.assert_not_called()

    def test_missing_or_invalid_automation_config_fails_closed(self):
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / 'automation.json'
            self.assertFalse(tiktok_schedule.automation_enabled(config_path))
            config_path.write_text('{invalid', encoding='utf-8')
            self.assertFalse(tiktok_schedule.automation_enabled(config_path))


if __name__ == '__main__':
    unittest.main()
