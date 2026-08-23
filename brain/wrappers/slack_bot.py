"""
Slack bot surface — events, OAuth, and @-mention answering.

Mount point: /slack  (all Slack endpoints live here)

OAuth flow:
  GET  /slack/install           → redirect to Slack OAuth URL
  GET  /slack/oauth/callback    → exchange code, store bot token, link workspace

Events (Slack Events API):
  POST /slack/events            → handle url_verification + app_mention + message.im

Identity linking:
  When a Slack user interacts, look up slack_user_links by (slack_user_id, slack_team_id).
  If found → use that user's account.
  If not found → look up user by email (from Slack profile) → link if match, else reply
  asking them to sign up via web first.

Replay protection:
  _verify_slack_signature rejects events with timestamps older than 5 minutes.

Background processing:
  POST /slack/events returns 200 immediately; actual Brain search runs in a daemon thread.
"""
import hashlib
import hmac
import os
import sys
import threading
import time
import uuid

import requests as _requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

slack_router = APIRouter()

# ── env vars ──────────────────────────────────────────────────────────────────
_CLIENT_ID      = lambda: os.getenv("SLACK_CLIENT_ID", "")
_CLIENT_SECRET  = lambda: os.getenv("SLACK_CLIENT_SECRET", "")
_SIGNING_SECRET = lambda: os.getenv("SLACK_SIGNING_SECRET", "")

_SCOPES = (
    "channels:history,channels:read,groups:history,groups:read,"
    "users:read,users:read.email,chat:write,app_mentions:read,"
    "im:history,im:write"
)


# ── signature verification ────────────────────────────────────────────────────

def _verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Verify the X-Slack-Signature HMAC-SHA256.
    Returns False if the timestamp is > 5 minutes old (replay protection).
    """
    signing_secret = _SIGNING_SECRET()
    if not signing_secret:
        # In dev without a signing secret configured, skip verification
        return True
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > 300:
        return False
    base = f"v0:{timestamp}:{request_body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_connect():
    from brain.core import db_pg as _pg
    return _pg.connect()


def _resolve_slack_identity(slack_user_id: str, slack_team_id: str, bot_token: str):
    """
    Return (user_id, account_id) for a Slack user, creating a link if needed.
    Returns None if the Slack user has no matching Thread account.
    Never logs email addresses or tokens.
    """
    from brain.auth import _BYPASS, _default_identity

    # Dev bypass: return default identity so events still work locally
    if _BYPASS:
        di = _default_identity()
        return (di.user_id, None)

    conn = _db_connect()
    try:
        cur = conn.cursor()

        # 1. Check existing link
        cur.execute(
            """
            SELECT user_id, account_id
            FROM slack_user_links
            WHERE slack_user_id = %s AND slack_team_id = %s
            LIMIT 1
            """,
            (slack_user_id, slack_team_id),
        )
        row = cur.fetchone()
        if row:
            return (row["user_id"], row["account_id"])

        # 2. Fetch profile from Slack
        try:
            resp = _requests.get(
                "https://slack.com/api/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": slack_user_id},
                timeout=10,
            )
            data = resp.json()
        except Exception:
            return None

        if not data.get("ok"):
            return None

        profile = data.get("user", {}).get("profile", {})
        email = (profile.get("email") or "").lower().strip()
        if not email:
            return None

        # 3. Look up membership by email — scan memberships joined with no user table
        # (we store user_id = clerk sub, not email; look up by email match in invitations
        # or a direct memberships scan — in practice we find active users by email via
        # the fact that Clerk embeds email in JWT; here we match invitations.email)
        cur.execute(
            """
            SELECT m.user_id, m.account_id
            FROM memberships m
            WHERE m.status = 'active'
              AND EXISTS (
                SELECT 1 FROM invitations i
                WHERE i.account_id = m.account_id
                  AND lower(i.email) = %s
                  AND i.status = 'accepted'
              )
            LIMIT 1
            """,
            (email,),
        )
        member_row = cur.fetchone()

        if not member_row:
            return None

        user_id    = member_row["user_id"]
        account_id = member_row["account_id"]

        # 4. Create the link
        cur.execute(
            """
            INSERT INTO slack_user_links
                (id, user_id, account_id, slack_user_id, slack_team_id, slack_email)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (slack_user_id, slack_team_id) DO NOTHING
            """,
            (str(uuid.uuid4()), user_id, account_id, slack_user_id, slack_team_id, email),
        )
        conn.commit()
        return (user_id, account_id)
    except Exception as exc:
        print(f"[slack_bot] _resolve_slack_identity error: {exc}", file=sys.stderr)
        return None
    finally:
        conn.close()


def _get_workspace_bot(account_id: str):
    """Return (bot_token, slack_team_id, bot_user_id) for the account, or None."""
    conn = _db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT bot_token, slack_team_id, bot_user_id
            FROM slack_workspace_integrations
            WHERE account_id = %s
            LIMIT 1
            """,
            (account_id,),
        )
        row = cur.fetchone()
        if row:
            return (row["bot_token"], row["slack_team_id"], row["bot_user_id"])
        return None
    except Exception:
        return None
    finally:
        conn.close()


def _get_integration_by_team(slack_team_id: str):
    """Return the integration row for a Slack team ID, or None."""
    conn = _db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, account_id, bot_token, bot_user_id
            FROM slack_workspace_integrations
            WHERE slack_team_id = %s
            LIMIT 1
            """,
            (slack_team_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _get_channel_mapping(account_id: str, channel_id: str):
    """Return (team_id, sync_members, slack_team_id) for a mapped channel, or None."""
    conn = _db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT team_id, sync_members, slack_team_id
            FROM slack_channel_mappings
            WHERE account_id = %s AND channel_id = %s
            LIMIT 1
            """,
            (account_id, channel_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _post_message(bot_token: str, channel: str, text: str, thread_ts: str = None):
    """Send a message via Slack chat.postMessage. Never logs text."""
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        _requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json=payload,
            timeout=10,
        )
    except Exception as exc:
        print(f"[slack_bot] _post_message failed: {exc}", file=sys.stderr)


# ── event processing (runs in background thread) ──────────────────────────────

def _handle_app_mention(event: dict, integration: dict):
    """Process an @-mention event: search Brain and reply in-thread."""
    slack_user_id = event.get("user", "")
    channel       = event.get("channel", "")
    thread_ts     = event.get("ts")
    raw_text      = event.get("text", "")
    slack_team_id = integration["slack_team_id"]
    account_id    = integration["account_id"]
    bot_token     = integration["bot_token"]

    # Strip the @mention from the text (format: <@BOTID> query text)
    import re as _re
    query = _re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()
    if not query:
        return

    # Resolve channel → team_id
    mapping = _get_channel_mapping(account_id, channel)
    if not mapping:
        _post_message(
            bot_token, channel,
            "This channel isn't connected to Thread. Ask your admin to map it.",
            thread_ts=thread_ts,
        )
        return

    team_id = mapping["team_id"]

    # Resolve Slack user → our identity
    identity_tuple = _resolve_slack_identity(slack_user_id, slack_team_id, bot_token)
    if not identity_tuple:
        _post_message(
            bot_token, channel,
            "I can't find your Thread account. Please sign up at the web app first.",
            thread_ts=thread_ts,
        )
        return

    user_id, _ = identity_tuple

    try:
        from brain.engine import Brain
        from brain.core import db_pg as _pg
        b = Brain(
            workspace_id=account_id,
            team_id=team_id,
            caller=user_id,
        )
        _pg._set_ws(b.conn, b.ws)
        result = b.search(query, surface="slack")
        answer = result.get("answer") or result.get("summary") or "No results found."
    except Exception as exc:
        print(f"[slack_bot] Brain search error: {exc}", file=sys.stderr)
        answer = "Sorry, I encountered an error searching Thread."

    _post_message(bot_token, channel, answer, thread_ts=thread_ts)


def _handle_im(event: dict, integration: dict):
    """Process a DM to the bot: search Brain with full principal scope and reply."""
    slack_user_id = event.get("user", "")
    channel       = event.get("channel", "")
    raw_text      = event.get("text", "").strip()
    slack_team_id = integration["slack_team_id"]
    account_id    = integration["account_id"]
    bot_token     = integration["bot_token"]

    if not raw_text:
        return

    identity_tuple = _resolve_slack_identity(slack_user_id, slack_team_id, bot_token)
    if not identity_tuple:
        _post_message(
            bot_token, channel,
            "I can't find your Thread account. Please sign up at the web app first, then DM me again.",
        )
        return

    user_id, _ = identity_tuple

    try:
        from brain.engine import Brain
        from brain.core import db_pg as _pg
        b = Brain(
            workspace_id=account_id,
            caller=user_id,
        )
        _pg._set_ws(b.conn, b.ws)
        result = b.search(raw_text, surface="slack")
        answer = result.get("answer") or result.get("summary") or "No results found."
    except Exception as exc:
        print(f"[slack_bot] Brain DM search error: {exc}", file=sys.stderr)
        answer = "Sorry, I encountered an error searching Thread."

    _post_message(bot_token, channel, answer)


def _dispatch_event(payload: dict):
    """Route an incoming Slack event to the appropriate handler."""
    event = payload.get("event", {})
    event_type = event.get("type", "")
    slack_team_id = payload.get("team_id", "")

    integration = _get_integration_by_team(slack_team_id)
    if not integration:
        return  # unknown workspace — silently drop

    if event_type == "app_mention":
        _handle_app_mention(event, integration)
    elif event_type == "message" and event.get("channel_type") == "im":
        # Ignore bot's own messages
        if event.get("bot_id"):
            return
        _handle_im(event, integration)


# ── OAuth endpoints ───────────────────────────────────────────────────────────

@slack_router.get("/install")
def slack_install(account_id: str = ""):
    """Redirect the browser to Slack's OAuth authorization page."""
    client_id = _CLIENT_ID()
    if not client_id:
        raise HTTPException(400, "SLACK_CLIENT_ID is not configured")

    state = account_id or "unknown"
    redirect_uri = os.getenv("SLACK_REDIRECT_URI", "")
    params = f"client_id={client_id}&scope={_SCOPES}&state={state}"
    if redirect_uri:
        params += f"&redirect_uri={redirect_uri}"
    url = f"https://slack.com/oauth/v2/authorize?{params}"
    return RedirectResponse(url)


@slack_router.get("/oauth/callback", response_class=HTMLResponse)
def slack_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Exchange the OAuth code for a bot token and store the workspace integration."""
    if error:
        return HTMLResponse(
            f"<h2>Slack connection failed</h2><p>{error}</p>", status_code=400
        )
    if not code:
        raise HTTPException(400, "Missing OAuth code")

    client_id     = _CLIENT_ID()
    client_secret = _CLIENT_SECRET()
    if not client_id or not client_secret:
        raise HTTPException(500, "Slack app credentials not configured")

    # Exchange code for token
    redirect_uri = os.getenv("SLACK_REDIRECT_URI", "")
    payload = {
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
    }
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri

    try:
        resp = _requests.post(
            "https://slack.com/api/oauth.v2.access",
            data=payload,
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        raise HTTPException(502, f"Slack API error: {exc}")

    if not data.get("ok"):
        raise HTTPException(400, f"Slack OAuth error: {data.get('error', 'unknown')}")

    bot_token     = data.get("access_token", "")
    bot_user_id   = (data.get("bot_user_id") or
                     data.get("authed_user", {}).get("id", ""))
    slack_team    = data.get("team", {})
    slack_team_id = slack_team.get("id", "")
    slack_name    = slack_team.get("name", "")
    account_id    = state  # account_id was passed as state param

    if not bot_token or not slack_team_id:
        raise HTTPException(400, "Incomplete response from Slack")

    # Upsert integration row
    conn = _db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO slack_workspace_integrations
                (id, account_id, slack_team_id, slack_team_name,
                 bot_token, bot_user_id, installed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slack_team_id) DO UPDATE SET
                account_id      = EXCLUDED.account_id,
                slack_team_name = EXCLUDED.slack_team_name,
                bot_token       = EXCLUDED.bot_token,
                bot_user_id     = EXCLUDED.bot_user_id,
                installed_by    = EXCLUDED.installed_by,
                created_at      = now()
            """,
            (str(uuid.uuid4()), account_id, slack_team_id, slack_name,
             bot_token, bot_user_id, account_id),
        )
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(500, f"DB error: {exc}")
    finally:
        conn.close()

    return HTMLResponse(
        "<h2>Slack connected!</h2>"
        "<p>Thread is now connected to your Slack workspace. "
        "You can close this tab and return to the app.</p>"
    )


# ── Events endpoint ───────────────────────────────────────────────────────────

@slack_router.post("/events")
async def slack_events(request: Request):
    """
    Slack Events API endpoint.
    Verifies HMAC signature, handles url_verification challenge, then dispatches
    app_mention and im events to background threads.
    Always returns 200 immediately — Slack retries on non-200.
    """
    body = await request.body()

    # Signature verification
    ts  = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, ts, sig):
        # Return 200 to avoid Slack retries, but log the rejection
        print("[slack_bot] signature verification failed — ignoring event", file=sys.stderr)
        return {"ok": False}

    try:
        import json as _json
        payload = _json.loads(body)
    except Exception:
        return {"ok": False}

    # Handle url_verification challenge (Slack sends this when you first add the endpoint)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    # Dispatch the event in a background thread so we return < 3s
    threading.Thread(
        target=_dispatch_event,
        args=(payload,),
        daemon=True,
    ).start()

    return {"ok": True}


# ── member sync ───────────────────────────────────────────────────────────────

def sync_channel_members(account_id: str, channel_id: str, team_id: str, bot_token: str):
    """
    Sync Slack channel members into Thread department memberships.
    Fetches current channel members from Slack, resolves each to our user_id
    via slack_user_links, and ensures a memberships row exists for team_id.
    Only called when sync_members=True on the channel mapping.
    """
    # 1. Fetch member IDs from Slack
    slack_members = []
    cursor = None
    while True:
        params: dict = {"channel": channel_id, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = _requests.get(
                "https://slack.com/api/conversations.members",
                headers={"Authorization": f"Bearer {bot_token}"},
                params=params,
                timeout=10,
            )
            data = resp.json()
        except Exception:
            break
        if not data.get("ok"):
            break
        slack_members.extend(data.get("members", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    if not slack_members:
        return {"synced": 0}

    # 2. Resolve each Slack user to our user_id and upsert membership
    conn = _db_connect()
    synced = 0
    try:
        cur = conn.cursor()
        # Bulk-fetch existing links for this team
        cur.execute(
            """
            SELECT slack_user_id, user_id
            FROM slack_user_links
            WHERE account_id = %s
            """,
            (account_id,),
        )
        link_map = {r["slack_user_id"]: r["user_id"] for r in cur.fetchall()}

        for slack_uid in slack_members:
            our_user_id = link_map.get(slack_uid)
            if not our_user_id:
                continue
            try:
                cur.execute(
                    """
                    INSERT INTO memberships (id, user_id, account_id, role, team_id, status)
                    VALUES (%s, %s, %s, 'employee', %s, 'active')
                    ON CONFLICT (user_id, account_id, team_id) DO UPDATE
                        SET status = 'active'
                    """,
                    (str(uuid.uuid4()), our_user_id, account_id, team_id),
                )
                synced += 1
            except Exception as exc:
                print(f"[slack_bot] sync_channel_members upsert error: {exc}", file=sys.stderr)

        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        print(f"[slack_bot] sync_channel_members error: {exc}", file=sys.stderr)
    finally:
        conn.close()

    return {"synced": synced}
