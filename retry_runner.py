"""Retry transient cloud publishing failures without risking duplicate uploads.

The cloud runner writes an 'uploading' record to the durable ledger before
sending media. Retrying an uncertain upload therefore stops safely in the
runner instead of sending the same video twice. This wrapper retries one time
to recover from temporary source/API failures and lets the workflow report only
the final outcome.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time


PUBLISH_MODES = {"publish", "scheduled"}
ALL_MODES = PUBLISH_MODES | {"preview"}
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 30


def run_with_retries(mode, count=1, max_attempts=MAX_ATTEMPTS,
                     retry_delay_seconds=RETRY_DELAY_SECONDS,
                     command_runner=None, sleep=None, env=None):
    """Run the cloud pipeline with at most one safe retry for publish modes."""
    if mode not in ALL_MODES:
        raise ValueError("Unsupported runner mode")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
        raise ValueError("Post count must be between 1 and 5")
    if mode != "publish" and count != 1:
        raise ValueError("Only publish mode supports multiple posts")
    if max_attempts not in (1, MAX_ATTEMPTS):
        raise ValueError("The retry limit must be one or two attempts")
    if (isinstance(retry_delay_seconds, bool) or
            not isinstance(retry_delay_seconds, (int, float)) or
            retry_delay_seconds < 0):
        raise ValueError("Retry delay must be a non-negative number")

    attempts = max_attempts if mode in PUBLISH_MODES else 1
    child_env = dict(os.environ if env is None else env)
    child_env["QURAN_BOT_DEFER_FAILURE_NOTICE"] = "1"
    command = [
        sys.executable,
        str(Path(__file__).with_name("cloud_runner.py")),
        mode,
        "--count",
        str(count),
    ]
    runner = command_runner or subprocess.run
    pause = sleep or time.sleep

    for attempt in range(1, attempts + 1):
        try:
            result = runner(command, env=child_env, check=False)
        except OSError as error:
            print("Cloud runner could not start: " + type(error).__name__, flush=True)
            return 127
        code = result.returncode
        if code == 0:
            return 0
        if attempt < attempts:
            print(
                f"Publishing attempt {attempt}/{attempts} failed (exit {code}); "
                f"retrying once in {retry_delay_seconds:g}s. The durable ledger "
                "prevents duplicate upload if the previous outcome is uncertain.",
                flush=True,
            )
            pause(retry_delay_seconds)
        else:
            print(f"Publishing failed after {attempts} safe attempt(s).", flush=True)
    return code if code > 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=sorted(ALL_MODES))
    parser.add_argument("--count", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args(argv)
    if args.mode != "publish" and args.count != 1:
        parser.error("--count is only supported for publish")
    return run_with_retries(args.mode, args.count)


if __name__ == "__main__":
    raise SystemExit(main())
