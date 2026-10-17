"""Combine related PR events into a single Discord message.

The DO function keeps no state between invocations, so Discord itself is the state: each grouped
event looks for the PR's recent message in the channel (found by its footer key) and edits it,
instead of posting a new message.

Groups, per PR:
- activity: opened, review_requested, synchronize. Reviewers and a push counter accumulate as fields.
- merge: approved review, auto_merge_enabled, enqueued, dequeued, merged. A timeline accumulates.

GitHub often sends a burst of these in the same second, handled by parallel invocations. To keep that
from producing duplicates or losing updates: a random delay spreads them out, edits are read back and
redone if another invocation overwrote them, and when two invocations both post a new message the
newer one merges itself into the older one and deletes itself.

requests + stdlib only: the DO function runs on the python:3.11 runtime.
"""

import os
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DISCORD_API = "https://discord.com/api/v10"

# Same colors as __main__.py
_COLOR_GREEN = 0x2ECC71
_COLOR_RED = 0xCB2431
_COLOR_PURPLE = 0x6F42C1
_COLOR_GRAY = 0x24292E
_COLOR_BLUE = 0x0075CA

_REVIEWERS = "Reviewers"
_PUSHES = "Pushes"
_MERGE_SUFFIX = " · merge"

# Merge flow states as shown in the title, with how far along each one is
_MERGE_STATES = {
    "approved": (1, _COLOR_GREEN),
    "auto-merge enabled": (2, _COLOR_BLUE),
    "added to merge queue": (3, _COLOR_BLUE),
    "removed from merge queue": (3, _COLOR_RED),
    "merged": (9, _COLOR_PURPLE),
}
# Queue moves happen in order and minutes apart, so the latest one is the current state
_QUEUE_STATES = {"added to merge queue", "removed from merge queue", "merged"}


def _now() -> datetime:
    return datetime.now(UTC)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class DiscordClient:
    def __init__(self) -> None:
        # requests is preinstalled in the DO python runtime; anything else would need a build.sh
        import requests

        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bot {os.environ['DISCORD_TOKEN']}"

    def _call(self, method: str, path: str, **kwargs) -> dict | list | None:
        resp = self._session.request(method, f"{DISCORD_API}{path}", timeout=10, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def get_messages(self, channel_id: str) -> list[dict]:
        return self._call("GET", f"/channels/{channel_id}/messages", params={"limit": 50})

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return self._call("GET", f"/channels/{channel_id}/messages/{message_id}")

    def post(self, channel_id: str, embed: dict) -> dict:
        return self._call("POST", f"/channels/{channel_id}/messages", json={"embeds": [embed]})

    def patch(self, channel_id: str, message_id: str, embed: dict) -> dict:
        return self._call("PATCH", f"/channels/{channel_id}/messages/{message_id}", json={"embeds": [embed]})

    def delete(self, channel_id: str, message_id: str) -> None:
        self._call("DELETE", f"/channels/{channel_id}/messages/{message_id}")


# ---------------------------------------------------------------------------
# What a grouped event contributes
# ---------------------------------------------------------------------------


@dataclass
class Update:
    footer: str  # identifies the PR + group; also visible under the embed
    fresh: dict | None  # embed to post when there is no recent message to edit
    reviewers: list[str]
    pushes: int
    line: str | None  # merge timeline line
    state: str | None  # merge state for the title
    opened: dict | None  # an "opened" embed takes over title/description/color of an earlier message

    def apply(self, existing: dict | None) -> dict | None:
        """Return the embed to write, or None if the existing one already has everything."""
        embed = dict(existing) if existing else dict(self.fresh or {})
        if not embed:
            return None
        if existing and self.opened:
            embed.update({k: self.opened[k] for k in ("title", "color", "url", "description") if k in self.opened})

        fields = {f["name"]: f["value"] for f in embed.get("fields", [])}
        if self.reviewers:
            current = [r for r in fields.get(_REVIEWERS, "").split(", ") if r]
            fields[_REVIEWERS] = ", ".join(current + [r for r in self.reviewers if r not in current])
        if self.pushes and existing:
            fields[_PUSHES] = f"+{int(fields.get(_PUSHES, '+0').lstrip('+')) + self.pushes}"
        if fields:
            embed["fields"] = [{"name": k, "value": v, "inline": True} for k, v in fields.items()]

        if self.line:
            lines = [ln for ln in (embed.get("description") or "").split("\n") if ln]
            if self.line not in lines:
                lines.append(self.line)
            embed["description"] = "\n".join(lines)[:4096]
        if self.state and existing:
            current = _merge_state(embed.get("title", ""))
            if current != "merged" and (
                self.state in _QUEUE_STATES or _MERGE_STATES[self.state][0] > _MERGE_STATES.get(current, (0,))[0]
            ):
                embed["title"] = _retitle(embed["title"], current, self.state)
                embed["color"] = _MERGE_STATES[self.state][1]

        embed["footer"] = {"text": self.footer}
        embed = _writable(embed)
        return None if existing and embed == _writable(existing) else embed

    def present_in(self, embed: dict) -> bool:
        """Whether this update survived in the embed (another invocation may have overwritten it)."""
        fields = {f["name"]: f["value"] for f in embed.get("fields", [])}
        reviewers = fields.get(_REVIEWERS, "").split(", ")
        lines = (embed.get("description") or "").split("\n")
        return all(r in reviewers for r in self.reviewers) and (not self.line or self.line in lines)


def _writable(embed: dict) -> dict:
    """Only the parts we set: embeds read back from Discord also carry read-only keys (type, proxy urls)."""
    result = {k: embed[k] for k in ("title", "description", "url", "color") if embed.get(k)}
    if embed.get("author"):
        result["author"] = {k: v for k, v in embed["author"].items() if k in ("name", "url", "icon_url") and v}
    if embed.get("fields"):
        result["fields"] = [{k: f[k] for k in ("name", "value", "inline") if k in f} for f in embed["fields"]]
    if (embed.get("footer") or {}).get("text"):
        result["footer"] = {"text": embed["footer"]["text"]}
    return result


def _merge_state(title: str) -> str | None:
    for state in _MERGE_STATES:
        if f"Pull request {state}:" in title:
            return state
    return None


def _retitle(title: str, current: str | None, new: str) -> str:
    if current:
        return title.replace(f"Pull request {current}:", f"Pull request {new}:", 1)
    return title


def _logins(users: list[dict], key: str) -> list[str]:
    return [u[key] for u in users if u.get(key)]


def build_update(event: str, args: dict, owner: str, author: dict) -> Update | None:
    """The grouped contribution of this event, or None if the event isn't grouped."""
    pr = args.get("pull_request") or {}
    action = args.get("action")
    if not pr or "number" not in pr:
        return None

    repo = args.get("repository", {}).get("name", "?")
    n, pr_title, url = pr["number"], pr.get("title", "?"), pr.get("html_url", "")
    key = f"{owner}/{repo}#{n}"
    sender = args.get("sender", {}).get("login", "?")

    def embed(color: int, what: str, description: str = "") -> dict:
        e = {"color": color, "author": author, "title": f"[{repo}] Pull request {what}: #{n} {pr_title}", "url": url}
        if description:
            e["description"] = description[:2048]
        return e

    def update(**kwargs) -> Update:
        defaults = {"reviewers": [], "pushes": 0, "line": None, "state": None, "opened": None}
        return Update(**(defaults | kwargs))

    # The payload carries the PR's full current reviewer list, so each event can restore names that
    # a racing invocation lost.
    all_reviewers = _logins(pr.get("requested_reviewers", []), "login") + _logins(pr.get("requested_teams", []), "name")

    if event == "pull_request" and action == "opened":
        opened = embed(_COLOR_GREEN, "opened", (pr.get("body") or "")[:300])
        return update(footer=key, fresh=opened, reviewers=all_reviewers, opened=opened)

    if event == "pull_request" and action == "review_requested":
        requested = args.get("requested_reviewer", {}).get("login") or args.get("requested_team", {}).get("name")
        reviewers = all_reviewers + ([requested] if requested and requested not in all_reviewers else [])
        return update(footer=key, fresh=embed(_COLOR_BLUE, "review requested"), reviewers=reviewers)

    if event == "pull_request" and action == "synchronize":
        return update(footer=key, fresh=embed(_COLOR_GRAY, "updated with new commits"), pushes=1)

    merge_key = key + _MERGE_SUFFIX
    if event == "pull_request_review" and action == "submitted":
        review = args.get("review", {})
        if review.get("state", "").lower() != "approved":
            return None
        reviewer = review.get("user", {}).get("login") or sender
        body = " ".join((review.get("body") or "").split())
        line = f"✅ Approved by {reviewer}" + (f": {body[:200]}" if body else "")
        return update(footer=merge_key, fresh=embed(_COLOR_GREEN, "approved", line), line=line, state="approved")

    merged_line = f"🟣 Merged by {(pr.get('merged_by') or {}).get('login') or sender}"
    if event != "pull_request":
        return None
    if action == "closed" and pr.get("merged"):
        state, line = "merged", merged_line
    elif action == "dequeued" and pr.get("merged"):
        # Leaving the queue because it merged: same contribution as the merged event itself
        state, line = "merged", merged_line
    elif action == "auto_merge_enabled":
        state, line = "auto-merge enabled", f"🤖 Auto-merge enabled by {sender}"
    elif action == "enqueued":
        state, line = "added to merge queue", "⏳ Added to merge queue"
    elif action == "dequeued":
        reason = (args.get("reason") or "").replace("_", " ").lower()
        state, line = "removed from merge queue", "↩️ Removed from merge queue" + (f" ({reason})" if reason else "")
    else:
        return None
    return update(footer=merge_key, fresh=embed(_MERGE_STATES[state][1], state, line), line=line, state=state)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _last_activity(message: dict) -> datetime:
    return datetime.fromisoformat(message.get("edited_timestamp") or message["timestamp"])


def _footer(message: dict) -> str | None:
    embeds = message.get("embeds") or [{}]
    return (embeds[0].get("footer") or {}).get("text")


def _recent(messages: list[dict], footer: str, window: timedelta) -> list[dict]:
    """Messages for this PR + group still inside the window, oldest first."""
    cutoff = _now() - window
    found = [m for m in messages if _footer(m) == footer and _last_activity(m) >= cutoff]
    return sorted(found, key=lambda m: int(m["id"]))


def deliver(client, channel_id: str, update: Update, window: timedelta) -> str:
    # Same-second bursts arrive as parallel invocations; spread them out before reading the channel
    _sleep(random.uniform(0.5, 3))

    try:
        recent = _recent(client.get_messages(channel_id), update.footer, window)
    except Exception as e:
        # e.g. missing Read Message History: post rather than drop the event
        embed = update.apply(None)
        client.post(channel_id, embed)
        return f"Could not read channel {channel_id} ({e}); posted a new message"

    if recent:
        return _edit(client, channel_id, recent[-1], update)

    posted = client.post(channel_id, update.apply(None))

    # Another invocation may have posted for the same PR at the same time: the oldest message wins
    try:
        recent = _recent(client.get_messages(channel_id), update.footer, window)
    except Exception:
        return f"Posted new message {posted['id']} to channel {channel_id}"
    oldest = recent[0] if recent else posted
    if oldest["id"] == posted["id"]:
        return f"Posted new message {posted['id']} to channel {channel_id}"
    result = _edit(client, channel_id, oldest, update)
    client.delete(channel_id, posted["id"])
    return f"{result} (removed duplicate {posted['id']})"


def _edit(client, channel_id: str, message: dict, update: Update) -> str:
    embed = update.apply(message["embeds"][0])
    if embed is None:
        return f"Message {message['id']} in channel {channel_id} already up to date"

    for _ in range(3):
        client.patch(channel_id, message["id"], embed)
        # Read back: a parallel invocation working from an older copy may have overwritten this edit
        _sleep(1)
        message = client.get_message(channel_id, message["id"])
        if update.present_in(message["embeds"][0]):
            break
        embed = update.apply(message["embeds"][0])
        if embed is None:
            break
    return f"Updated message {message['id']} in channel {channel_id}"
