"""
Integration connector — 1,047+ app integrations.

Internal implementation detail. Never expose vendor name to users.
"""
import os, datetime, time
from .base import Connector

# Thread-local billing context set by the API layer
_billing_ctx: dict = {}

def set_billing_context(account_id: str):
    _billing_ctx["account_id"] = account_id

def clear_billing_context():
    _billing_ctx.clear()

def _account_id() -> str:
    return _billing_ctx.get("account_id", "")

_BASE = "https://backend.composio.dev/api/v3.1"

# Actions to pull per app during backfill/poll.
# Maps composio app slug → list of action slugs to execute.
# Add more from: https://composio.dev/toolkits
_APP_ACTIONS = {
    "gmail": [
        ("GMAIL_FETCH_EMAILS", {"max_results": 100, "query": "category:primary"}),
    ],
    "slack": [
        ("SLACK_LIST_CHANNELS", {}),
        ("SLACK_FETCH_MESSAGES", {"limit": 100}),
    ],
    "notion": [
        ("NOTION_SEARCH_NOTION_PAGE", {"query": "", "page_size": 50}),
    ],
    "googlecalendar": [
        ("GOOGLECALENDAR_LIST_CALENDARS", {}),
        ("GOOGLECALENDAR_GET_CURRENT_DATE_TIME", {}),
    ],
    "googledrive": [
        ("GOOGLEDRIVE_LIST_FILES", {"mime_type": "application/vnd.google-apps.document"}),
    ],
    "github": [
        ("GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER", {"per_page": 50}),
        ("GITHUB_LIST_ISSUES_BY_REPO", {"per_page": 50}),
    ],
    "linear": [
        ("LINEAR_LIST_ISSUES", {"first": 50}),
    ],
    "jira": [
        ("JIRA_GET_ALL_ISSUES_FROM_A_PROJECT", {"max_results": 50}),
    ],
    "hubspot": [
        ("HUBSPOT_LIST_CONTACTS", {"limit": 50}),
    ],
    "notion": [
        ("NOTION_SEARCH_NOTION_PAGE", {"query": ""}),
    ],
}


from brain.core.resilience import retry, get_breaker

_composio_breaker = get_breaker("composio", failure_threshold=5, recovery_timeout=60.0)


def _headers():
    key = os.getenv("COMPOSIO_API_KEY", "")
    return {"x-api-key": key, "Content-Type": "application/json"}


@retry(max_attempts=3, base_delay=1.0, max_delay=16.0, jitter=0.4)
def _get(path, params=None):
    import requests
    with _composio_breaker:
        resp = requests.get(f"{_BASE}{path}", headers=_headers(), params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()


@retry(max_attempts=3, base_delay=1.0, max_delay=16.0, jitter=0.4)
def _post(path, body):
    import requests
    with _composio_breaker:
        resp = requests.post(f"{_BASE}{path}", headers=_headers(), json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()


def list_connected_apps(user_id: str = None):
    """Return list of apps connected in Composio.
    If user_id is given, only returns connections for that entity (per-user isolation)."""
    try:
        params = {}
        if user_id:
            params["user_id"] = user_id
        data = _get("/connected_accounts", params=params or None)
        return [
            {
                "id":       item.get("id") or item.get("nanoid", ""),
                "app":      (item.get("toolkit", {}).get("slug") or item.get("appName", "")).lower(),
                "appName":  item.get("toolkit", {}).get("name") or item.get("appName", ""),
                "name":     item.get("toolkit", {}).get("name") or item.get("appName", ""),
                "logo":     item.get("toolkit", {}).get("meta", {}).get("logo", ""),
                "status":   item.get("status", ""),
                "user_id":  item.get("userId") or item.get("user_id") or user_id or "default",
            }
            for item in (data.get("items") or [])
            if item.get("status", "").upper() in ("ACTIVE", "INITIATED", "CONNECTED")
        ]
    except Exception as e:
        raise RuntimeError(f"Composio API error: {e}")


def list_all_apps():
    """Return full catalog of available Composio apps (toolkits)."""
    try:
        data = _get("/toolkits", params={"limit": 200})
        return [
            {
                "slug":        item.get("slug", ""),
                "name":        item.get("name", ""),
                "logo":        (item.get("meta") or {}).get("logo", ""),
                "description": (item.get("meta") or {}).get("description", ""),
                "categories":  [(c.get("name","")) for c in (item.get("meta") or {}).get("categories", [])],
                "tools_count": (item.get("meta") or {}).get("tools_count", 0),
            }
            for item in (data.get("items") or [])
            if item.get("deprecated") is not True and not item.get("no_auth")
        ]
    except Exception as e:
        raise RuntimeError(f"Composio API error: {e}")


def _ensure_auth_config(app_slug: str) -> str:
    """Get or create a composio-managed auth_config for an app. Returns auth_config id."""
    # Check if one already exists
    data = _get("/auth_configs", params={"toolkit_slug": app_slug})
    items = data.get("items") or []
    if items:
        return items[0]["auth_config"]["id"]
    # Create a new managed auth config
    body = {
        "toolkit": {"slug": app_slug},
        "auth_config": {"type": "use_composio_managed_auth", "name": f"{app_slug}-managed"},
    }
    result = _post("/auth_configs", body)
    return result["auth_config"]["id"]


def disconnect_app(connected_account_id: str):
    """Delete a connected account by its ID."""
    import requests
    resp = requests.delete(
        f"{_BASE}/connected_accounts/{connected_account_id}",
        headers=_headers(), timeout=15
    )
    resp.raise_for_status()
    return {"ok": True}


def get_connect_url(app_slug: str, user_id: str = "default", redirect_url: str = None):
    """Initiate OAuth for an app. Returns a URL the user opens in browser."""
    try:
        auth_config_id = _ensure_auth_config(app_slug)
        body = {
            "auth_config_id": auth_config_id,
            "user_id": user_id,
            "callback_url": redirect_url or "http://localhost:7070",
        }
        data = _post("/connected_accounts/link", body)
        return data.get("redirect_url") or data.get("redirectUrl") or ""
    except Exception as e:
        raise RuntimeError(f"Could not initiate OAuth for {app_slug}: {e}")


def execute_action(action_slug: str, arguments: dict, entity_id: str = "default",
                   connected_account_id: str = None):
    """Execute a Composio action and return raw result."""
    body = {
        "arguments": arguments,
        "user_id": entity_id,
    }
    if connected_account_id:
        body["connectedAccountId"] = connected_account_id
    return _post(f"/tools/execute/{action_slug}", body)


def _normalize_result(app, action, raw_result) -> list:
    """Convert a Composio action result → list of Episode dicts."""
    episodes = []
    data = raw_result.get("response") or raw_result.get("data") or raw_result

    now = datetime.datetime.utcnow().isoformat()

    if app == "gmail":
        # GMAIL_FETCH_EMAILS returns a list of email objects
        emails = data if isinstance(data, list) else data.get("messages", [])
        for email in emails:
            subject = email.get("subject") or "(no subject)"
            sender  = email.get("sender") or email.get("from", "unknown")
            body    = email.get("messageText") or email.get("body", "")
            date    = email.get("timestamp") or email.get("date", now)
            msg_id  = email.get("messageId") or email.get("id", "")
            if not body or len(body) < 20:
                continue
            episodes.append({
                "source":      "composio:gmail",
                "external_id": f"gmail:{msg_id}",
                "title":       subject,
                "body":        f"From: {sender}\nSubject: {subject}\n\n{body[:2000]}",
                "author":      sender,
                "url":         f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
                "occurred_at": date,
                "acl":         ["everyone"],
            })

    elif app == "slack":
        messages = data if isinstance(data, list) else data.get("messages", [])
        for msg in messages:
            text    = msg.get("text") or msg.get("content", "")
            user    = msg.get("user") or msg.get("username", "unknown")
            ts      = msg.get("ts") or msg.get("timestamp", now)
            channel = msg.get("channel", "")
            if not text or len(text) < 10:
                continue
            episodes.append({
                "source":      "composio:slack",
                "external_id": f"slack:{channel}:{ts}",
                "title":       f"#{channel}",
                "body":        text,
                "author":      user,
                "url":         "",
                "occurred_at": ts,
                "acl":         ["everyone"],
            })

    elif app in ("notion",):
        pages = data if isinstance(data, list) else data.get("pages", data.get("results", []))
        for page in pages:
            title   = page.get("title") or page.get("name") or "(untitled)"
            content = page.get("content") or page.get("body") or page.get("text", "")
            url     = page.get("url", "")
            edited  = page.get("lastEditedTime") or page.get("last_edited_time", now)
            pid     = page.get("id", "")
            if not content or len(content) < 30:
                continue
            episodes.append({
                "source":      "composio:notion",
                "external_id": f"notion:{pid}",
                "title":       title,
                "body":        f"{title}\n\n{content[:3000]}",
                "author":      "notion-user",
                "url":         url,
                "occurred_at": edited,
                "acl":         ["everyone"],
            })

    elif app == "github":
        items = data if isinstance(data, list) else data.get("items", data.get("repositories", []))
        for item in items:
            title = item.get("full_name") or item.get("title") or item.get("name", "")
            body  = item.get("body") or item.get("description") or ""
            url   = item.get("html_url") or item.get("url", "")
            date  = item.get("updated_at") or item.get("created_at", now)
            iid   = str(item.get("id") or item.get("number", ""))
            if not title:
                continue
            episodes.append({
                "source":      "composio:github",
                "external_id": f"github:{iid}",
                "title":       title,
                "body":        f"{title}\n\n{body[:2000]}" if body else title,
                "author":      (item.get("user") or {}).get("login", ""),
                "url":         url,
                "occurred_at": date,
                "acl":         ["everyone"],
            })

    elif app in ("linear", "jira"):
        items = data if isinstance(data, list) else data.get("issues", data.get("nodes", []))
        for item in items:
            title  = item.get("title") or item.get("summary", "")
            desc   = item.get("description") or ""
            url    = item.get("url") or item.get("htmlUrl", "")
            date   = item.get("updatedAt") or item.get("updated", now)
            status = item.get("state", {})
            state  = (status.get("name") if isinstance(status, dict) else str(status)) or ""
            iid    = str(item.get("id") or item.get("key", ""))
            if not title:
                continue
            episodes.append({
                "source":      f"composio:{app}",
                "external_id": f"{app}:{iid}",
                "title":       title,
                "body":        f"{title} [{state}]\n\n{desc[:2000]}" if desc else f"{title} [{state}]",
                "author":      (item.get("assignee") or {}).get("name", "") if isinstance(item.get("assignee"), dict) else "",
                "url":         url,
                "occurred_at": date,
                "acl":         ["everyone"],
            })

    else:
        # Generic fallback — dump raw JSON as body so it still gets extracted
        import json
        body_text = json.dumps(data, indent=2)[:3000] if data else ""
        if body_text:
            episodes.append({
                "source":      f"composio:{app}",
                "external_id": f"composio:{app}:{action}:{int(time.time())}",
                "title":       f"{app} — {action}",
                "body":        body_text,
                "author":      app,
                "url":         "",
                "occurred_at": now,
                "acl":         ["everyone"],
            })

    return episodes


class ComposioConnector(Connector):
    """
    Universal connector backed by Composio's 1,047+ app catalog.

    Usage:
      - Set COMPOSIO_API_KEY in .env
      - Connect apps at composio.dev/dashboard
      - Call backfill() or sync() and all connected apps are ingested
    """
    source = "composio"

    def __init__(self, app: str = None, user_id: str = None):
        """
        app:     optional Composio app slug to restrict to one app (e.g. "gmail").
        user_id: the caller's user identity — used as Composio entity_id so each
                 user's OAuth connection is isolated from teammates.
        """
        if not os.getenv("COMPOSIO_API_KEY"):
            raise RuntimeError(
                "COMPOSIO_API_KEY is not set.\n"
                "Sign up at composio.dev and add your API key to .env"
            )
        self.app = app
        self.user_id = user_id or "default"
        self._connected = None

    def _connected_apps(self):
        if self._connected is None:
            # Filter to this user's connections only (per-user isolation)
            self._connected = list_connected_apps(user_id=self.user_id)
            # Fall back to all connections only if user has none of their own
            if not self._connected and self.user_id != "default":
                self._connected = list_connected_apps()
        return self._connected

    def backfill(self):
        return list(self._fetch(poll=False))

    def poll(self):
        return list(self._fetch(poll=True))

    def normalize(self, raw):
        """Webhook path: raw Composio event → Episode dict."""
        app = raw.get("app", "composio")
        action = raw.get("action", "")
        results = _normalize_result(app, action, raw.get("data", {}))
        return results[0] if results else {
            "source": f"composio:{app}", "external_id": "",
            "title": action, "body": str(raw), "author": app,
            "url": "", "occurred_at": datetime.datetime.utcnow().isoformat(),
            "acl": ["everyone"],
        }

    def _fetch(self, poll: bool):
        connected = self._connected_apps()
        if not connected:
            raise RuntimeError(
                "No apps connected yet. Connect an integration from the Integrations tab."
            )

        acct_id = _account_id()
        total_calls = 0
        total_items = 0

        # ACL: "everyone" for shared apps; caller's user_id for personal connections
        acl_owner = self.user_id if self.user_id != "default" else "everyone"

        for account in connected:
            app = account["app"]
            if self.app and app != self.app:
                continue
            # Use this account's owner as entity_id (falls back to self.user_id)
            entity_id = account.get("user_id") or self.user_id
            actions = _APP_ACTIONS.get(app, [])
            for action_slug, args in actions:
                try:
                    if poll and "query" in args:
                        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=3))
                        args = dict(args, query=f"after:{cutoff.strftime('%Y/%m/%d')}")
                    result = execute_action(action_slug, args, entity_id=entity_id)
                    total_calls += 1
                    eps = list(_normalize_result(app, action_slug, result))
                    total_items += len(eps)
                    for ep in eps:
                        # Tag with owner's ACL so only they can retrieve it
                        ep["acl"] = acl_owner
                        yield ep
                except Exception as e:
                    import sys
                    print(f"[integrations] {app}/{action_slug} error: {e}", file=sys.stderr)

        # Meter this run (fire-and-forget)
        try:
            from brain.core.db import BACKEND
            if BACKEND == "postgres" and acct_id:
                from brain.core.db_pg import record_integration_usage
                record_integration_usage(
                    account_id=acct_id,
                    provider="integrations",
                    api_calls=total_calls,
                    backfill_items=total_items if not poll else 0,
                    connected_accounts=len(connected),
                )
        except Exception as e:
            import sys
            print(f"[integrations] metering write failed: {e}", file=sys.stderr)
