"""Optional run-status delivery: Telegram, Discord and a dead-man's-switch ping.

Publishing must never depend on notifications. Every send is best-effort,
every failure is swallowed with a safe diagnostic, and all credentials arrive
through environment variables so nothing sensitive reaches Git, logs or the
rendered video.
"""
from __future__ import annotations

import argparse
import logging
import os

LOG = logging.getLogger("quran-bot.notify")

REQUEST_TIMEOUT = (10, 30)  # connect, read seconds
TELEGRAM_LIMIT = 3800       # Telegram allows 4096 characters per message
DISCORD_LIMIT = 1900        # Discord allows 2000 characters per message

KIND_LABELS = {
    "success": "✅ Quran Shorts",
    "failure": "❌ Quran Shorts",
    "safety": "🛡️ Quran Shorts safety",
    "health": "⚠️ Quran Shorts health",
    "info": "ℹ️ Quran Shorts",
}

# Delivered keys live only for the current process. Cross-run repetition is
# prevented by the ledger: health flags and safety incidents are recorded
# there before the next run can report them again.
_DELIVERED: set[str] = set()


class Config:
    """Notification settings read once from the environment."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.telegram_token = str(env.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.telegram_chat_id = str(env.get("TELEGRAM_CHAT_ID", "")).strip()
        self.discord_webhook = str(env.get("DISCORD_WEBHOOK_URL", "")).strip()
        self.heartbeat_url = str(env.get("HEARTBEAT_URL", "")).strip()
        self.run_link = str(env.get("NOTIFY_RUN_URL", "")).strip()

    @property
    def telegram_ready(self):
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def discord_ready(self):
        # Webhook URLs carry a secret in the path; plain HTTP would leak it.
        return self.discord_webhook.startswith("https://")

    @property
    def heartbeat_ready(self):
        # The ping URL contains an opaque secret; require HTTPS for the same reason.
        return self.heartbeat_url.startswith("https://")


def _post(url, body, session=None):
    import requests
    client = session or requests
    response = client.post(url, json=body, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400:
        # Never include the response body: it can echo tokens or infrastructure details.
        raise RuntimeError(f"Notification endpoint returned HTTP {response.status_code}")


def _telegram(config, text, session=None):
    _post(f"https://api.telegram.org/bot{config.telegram_token}/sendMessage",
          {"chat_id": config.telegram_chat_id, "text": text,
           "disable_web_page_preview": True}, session)


def _discord(config, text, session=None):
    _post(config.discord_webhook, {"content": text}, session)


def notify(text, kind="info", key=None, env=None, session=None):
    """Best-effort delivery of one message to every configured channel.

    Returns the list of channels the message reached. Never raises.
    """
    if key:
        if key in _DELIVERED:
            return []
        _DELIVERED.add(key)
    config = Config(env)
    channels = []
    if config.telegram_ready:
        channels.append("telegram")
    if config.discord_ready:
        channels.append("discord")
    if not channels:
        return []
    label = KIND_LABELS.get(kind, KIND_LABELS["info"])
    message = label + "\n" + text.strip()
    if config.run_link:
        message += "\n" + config.run_link
    delivered = []
    for channel in channels:
        try:
            if channel == "telegram":
                _telegram(config, message[:TELEGRAM_LIMIT], session)
            else:
                _discord(config, message[:DISCORD_LIMIT], session)
            delivered.append(channel)
        except Exception as exc:
            LOG.warning("Notification channel %s unavailable: %s", channel, type(exc).__name__)
    return delivered


def heartbeat(ok=True, env=None, session=None):
    """Ping a healthchecks.io-style URL so silence can be detected externally.

    The monitor alerts when scheduled pings stop. Returns True when a ping was
    accepted. Never raises.
    """
    import requests
    client = session or requests
    config = Config(env)
    if not config.heartbeat_ready:
        return False
    target = config.heartbeat_url.rstrip("/")
    if not ok:
        target += "/fail"
    try:
        response = client.get(target, timeout=REQUEST_TIMEOUT)
        if response.status_code >= 400:
            raise RuntimeError(f"Heartbeat endpoint returned HTTP {response.status_code}")
        return True
    except Exception as exc:
        LOG.warning("Heartbeat ping failed: %s", type(exc).__name__)
        return False


def main(argv=None):
    """Workflow entry point: report the job status and ping the monitor.

    This command must always exit 0; a broken notification channel must never
    fail the workflow step it reports on.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run-status"])
    parser.add_argument("--status", choices=["success", "failure", "cancelled"], required=True)
    parser.add_argument("--context", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ok = args.status == "success"
    if not ok:
        text = "Workflow run did not complete (status: " + args.status + ")."
        if args.context:
            text += "\n" + args.context
        try:
            notify(text, kind="failure", key="run-status")
        except Exception as exc:  # Defense in depth: notify() must never raise.
            LOG.warning("Run-status notification failed: %s", type(exc).__name__)
    try:
        heartbeat(ok=ok)
    except Exception as exc:  # Heartbeat failures must not fail the workflow.
        LOG.warning("Heartbeat ping failed: %s", type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
