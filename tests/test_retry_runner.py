from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import retry_runner


class RetryRunnerTests(unittest.TestCase):
    def test_scheduled_run_retries_once_and_returns_success(self):
        runner = Mock(side_effect=[
            SimpleNamespace(returncode=1),
            SimpleNamespace(returncode=0),
        ])
        pause = Mock()

        result = retry_runner.run_with_retries(
            "scheduled", command_runner=runner, sleep=pause, env={"SAFE": "yes"})

        self.assertEqual(result, 0)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.kwargs["env"]["SAFE"], "yes")
        self.assertEqual(runner.call_args.kwargs["env"]["QURAN_BOT_DEFER_FAILURE_NOTICE"], "1")
        pause.assert_called_once_with(30)

    def test_preview_is_run_only_once(self):
        runner = Mock(return_value=SimpleNamespace(returncode=1))
        pause = Mock()

        result = retry_runner.run_with_retries(
            "preview", command_runner=runner, sleep=pause, env={})

        self.assertEqual(result, 1)
        runner.assert_called_once()
        pause.assert_not_called()

    def test_final_failure_is_reported_after_single_retry(self):
        runner = Mock(side_effect=[
            SimpleNamespace(returncode=1),
            SimpleNamespace(returncode=7),
        ])
        pause = Mock()

        result = retry_runner.run_with_retries(
            "publish", command_runner=runner, sleep=pause, env={})

        self.assertEqual(result, 7)
        self.assertEqual(runner.call_count, 2)
        pause.assert_called_once_with(30)

    def test_retry_attempt_limit_cannot_exceed_two(self):
        with self.assertRaises(ValueError):
            retry_runner.run_with_retries("scheduled", max_attempts=3)

    def test_non_publish_modes_cannot_request_multiple_posts(self):
        with self.assertRaises(ValueError):
            retry_runner.run_with_retries("scheduled", count=2)


if __name__ == "__main__":
    unittest.main()
