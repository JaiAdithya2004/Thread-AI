"""
Real Slack connector — requires SLACK_BOT_TOKEN in .env
Scopes needed: channels:history, channels:read, groups:history,
               groups:read, users:read, im:history (optional)
"""
import os, datetime
from .base import Connector
from brain.core.resilience import get_breaker

_slack_breaker = get_breaker("slack", failure_threshold=5, recovery_timeout=60.0)

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SDK = True
except ImportError:
    _SDK = False


class SlackConnector(Connector):
    source = "slack"

    def __init__(self):
        self.token = os.getenv("SLACK_BOT_TOKEN")
        if not self.token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN is not set.\n"
                "Add it to your .env file: SLACK_BOT_TOKEN=xoxb-..."
            )
        if not _SDK:
            raise RuntimeError("slack_sdk is not installed. Run: pip install slack_sdk")
        self.client = WebClient(token=self.token)

    # ── public API ──────────────────────────────────────────────────────────
    def backfill(self):
        """Fetch full history from every channel the bot is in."""
        episodes = []
        for ch in self._list_channels():
            episodes.extend(self._fetch_channel(ch))
        return episodes

    def poll(self):
        """Fetch messages from the past 24 hours."""
        import time
        oldest = str(time.time() - 86_400)
        episodes = []
        for ch in self._list_channels():
            episodes.extend(self._fetch_channel(ch, oldest=oldest))
        return episodes

    def normalize(self, raw):
        """Webhook path: raw Slack event dict → Episode dict."""
        ch_id  = raw.get("channel", "")
        ch_name = raw.get("channel_name", ch_id)
        return self._normalize_msg(raw, ch_id, ch_name, acl=["everyone"]) or {}

    # ── internals ───────────────────────────────────────────────────────────
    def _list_channels(self):
        """Return all channels the bot is a member of."""
        channels, cursor = [], None
        while True:
            with _slack_breaker:
                resp = self.client.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    **({"cursor": cursor} if cursor else {}),
                )
            for ch in resp.get("channels", []):
                if ch.get("is_member"):
                    channels.append(ch)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return channels

    def _fetch_channel(self, channel, oldest=None):
        """Paginate conversations_history for one channel."""
        ch_id   = channel["id"]
        ch_name = channel.get("name", ch_id)
        is_priv = channel.get("is_private", False)
        acl     = self._members(ch_id) if is_priv else ["everyone"]

        episodes, cursor = [], None
        kwargs = {"channel": ch_id, "limit": 200}
        if oldest:
            kwargs["oldest"] = oldest

        while True:
            if cursor:
                kwargs["cursor"] = cursor
            try:
                with _slack_breaker:
                    resp = self.client.conversations_history(**kwargs)
            except SlackApiError as e:
                if e.response.get("error") == "not_in_channel":
                    break
                raise

            for msg in resp.get("messages", []):
                ep = self._normalize_msg(msg, ch_id, ch_name, acl)
                if ep:
                    episodes.append(ep)

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return episodes

    def _members(self, channel_id):
        """Return user IDs of a private channel's members (used as ACL)."""
        try:
            members, cursor = [], None
            while True:
                with _slack_breaker:
                    resp = self.client.conversations_members(
                        channel=channel_id, limit=200,
                        **({"cursor": cursor} if cursor else {}),
                    )
                members.extend(resp.get("members", []))
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            return members or ["everyone"]
        except Exception:
            return ["everyone"]

    def _normalize_msg(self, msg, ch_id, ch_name, acl):
        """Convert a Slack message dict → Episode dict. Returns None for noise."""
        # skip system messages and bots
        if msg.get("type") != "message":
            return None
        if msg.get("subtype") in ("channel_join", "channel_leave", "channel_archive",
                                  "channel_purpose", "channel_topic", "bot_message"):
            return None
        body = (msg.get("text") or "").strip()
        if not body:
            return None

        ts    = msg.get("ts", "")
        author = msg.get("user") or msg.get("username", "unknown")
        url   = f"https://slack.com/archives/{ch_id}/p{ts.replace('.', '')}"
        try:
            dt = datetime.datetime.fromtimestamp(float(ts)).isoformat()
        except Exception:
            dt = ts

        return {
            "source":      "slack",
            "external_id": f"{ch_id}:{ts}",
            "title":       f"#{ch_name}",
            "body":        body,
            "author":      author,
            "url":         url,
            "occurred_at": dt,
            "acl":         acl,
        }
