import copy
import unittest
from pathlib import Path
from unittest.mock import Mock

import reciter_watcher as watcher


class FakeContents:
    def __init__(self, catalog, state=None):
        self.documents = {
            watcher.CATALOG_PATH: copy.deepcopy(catalog),
            watcher.STATE_PATH: copy.deepcopy(state),
        }
        self.writes = []

    def read_json(self, path):
        document = self.documents[path]
        return copy.deepcopy(document), f"sha-{path}" if document is not None else None

    def write_json(self, path, document, sha, message):
        self.documents[path] = copy.deepcopy(document)
        self.writes.append((path, copy.deepcopy(document), message))
        return f"new-sha-{path}"


class ReciterWatcherTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "allowed_reciter_ids": [1, 2],
            "proposed_reciter_ids_for_review": [3],
            "blocked_reciter_ids": [9],
        }

    def test_parses_quran_foundation_reciter_listing(self):
        payload = {"recitations": [
            {"id": 2, "reciter_name": "Known"},
            {"id": 10, "reciter_name": "New voice"},
        ]}
        self.assertEqual(watcher.parse_reciters(payload), {2: "Known", 10: "New voice"})

    def test_detects_only_a_genuinely_new_reciter_id_and_name(self):
        current = {1: "One", 2: "Two", 3: "Already proposed",
                   9: "Blocked", 10: "New voice"}
        self.assertEqual(
            watcher.find_new_reciters(current, {1, 2, 3}, self.catalog),
            {10: "New voice"},
        )

    def test_ignores_reciters_already_allowed_proposed_blocked_or_seen(self):
        current = {1: "Allowed", 2: "Allowed", 3: "Proposed", 9: "Blocked", 12: "Seen"}
        self.assertEqual(
            watcher.find_new_reciters(current, {12}, self.catalog),
            {},
        )

    def test_first_check_saves_baseline_without_proposing_existing_reciters(self):
        store = FakeContents(self.catalog)
        result = watcher.run_check(store, fetcher=lambda: {1: "One", 12: "Twelve"})
        self.assertEqual(result["status"], "baseline")
        self.assertEqual(store.documents[watcher.CATALOG_PATH]["proposed_reciter_ids_for_review"], [3])
        self.assertEqual(store.documents[watcher.STATE_PATH]["reciter_ids"], [1, 12])
        self.assertEqual([row[0] for row in store.writes], [watcher.STATE_PATH])

    def test_new_reciter_is_proposed_but_never_added_to_allowed_list(self):
        state = {"schema": 1, "checked_at": "2026-01-01T00:00:00Z", "reciter_ids": [1, 2, 3]}
        store = FakeContents(self.catalog, state)
        notifications = Mock()
        result = watcher.run_check(
            store,
            fetcher=lambda: {1: "One", 2: "Two", 3: "Three", 11: "New Reciter"},
            notifier=notifications,
            notify_env={"DISCORD_WEBHOOK_URL": "https://discord.example/webhook"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["new_reciters"], {11: "New Reciter"})
        updated = store.documents[watcher.CATALOG_PATH]
        self.assertEqual(updated["allowed_reciter_ids"], [1, 2])
        self.assertEqual(updated["proposed_reciter_ids_for_review"], [3, 11])
        notifications.assert_called_once()
        self.assertIn("11", notifications.call_args.args[0])
        self.assertIn("New Reciter", notifications.call_args.args[0])
        self.assertIn("allowed_reciter_ids", notifications.call_args.args[0])

    def test_unavailable_api_is_skipped_without_advancing_state_or_failing(self):
        store = FakeContents(self.catalog, {
            "schema": 1, "checked_at": "2026-01-01T00:00:00Z", "reciter_ids": [1, 2, 3]
        })
        result = watcher.run_check(
            store,
            fetcher=Mock(side_effect=ConnectionError("private request details")),
            notifier=Mock(),
        )
        self.assertEqual(result["status"], "skipped")
        self.assertIn("ConnectionError", result["summary"])
        self.assertNotIn("private request details", result["summary"])
        self.assertEqual(store.writes, [])
        self.assertEqual(store.documents[watcher.CATALOG_PATH]["allowed_reciter_ids"], [1, 2])

    def test_failed_check_is_isolated_from_daily_publish_workflow(self):
        root = Path(__file__).resolve().parents[1]
        daily_workflow = (root / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
        weekly_workflow = (root / ".github" / "workflows" / "reciter-watcher.yml").read_text(encoding="utf-8")
        self.assertIn("Create or publish the next recording", daily_workflow)
        self.assertNotIn("reciter_watcher.py", daily_workflow)
        self.assertIn("reciter_watcher.py", weekly_workflow)


if __name__ == "__main__":
    unittest.main()
