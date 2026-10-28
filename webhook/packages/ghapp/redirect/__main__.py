import importlib
import os
import sys
import traceback
from datetime import timedelta
from pathlib import Path

DISCORD_API = "https://discord.com/api/v10"

_ignored_users = {
    "codecov[bot]",
    "dependabot[bot]",
    "pre-commit-ci[bot]",
    "renovate[bot]",
}

# Issue/PR actions that are just noise in a channel
_SKIPPED_ACTIONS = {"labeled", "unlabeled"}

# Colors matching Discord's GitHub integration
_COLOR_GREEN = 0x2ECC71  # opened / created
_COLOR_RED = 0xCB2431  # closed / deleted
_COLOR_PURPLE = 0x6F42C1  # merged
_COLOR_GRAY = 0x24292E  # push
_COLOR_BLUE = 0x0075CA  # comment / review


def _local(name: str):
    # Sibling modules are imported lazily so a packaging problem fails inside main()'s try (visible
    # in GitHub's Recent Deliveries) instead of killing the function at load time with no logs.
    here = str(Path(__file__).resolve().parent) if "__file__" in globals() else str(Path.cwd())
    if here not in sys.path:
        sys.path.insert(0, here)
    return importlib.import_module(name)


def _post_to_discord(channel_id: str, payload: dict) -> str:
    # requests is preinstalled in the DO python runtime; anything else would need a build.sh
    import requests

    token = os.environ["DISCORD_TOKEN"]
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    resp = requests.post(url, json=payload, headers={"Authorization": f"Bot {token}"}, timeout=10)
    resp.raise_for_status()
    return f"Posted to channel {channel_id}: {resp.status_code}"


def _author(args: dict) -> dict:
    sender = args.get("sender", {})
    login = sender.get("login", "?")
    return {
        "name": login,
        "url": f"https://github.com/{login}",
        "icon_url": sender.get("avatar_url", ""),
    }


def _embed(color: int, title: str, url: str, description: str, args: dict) -> dict:
    e: dict = {"color": color, "author": _author(args), "title": title, "url": url}
    if description:
        e["description"] = description[:2048]
    return e


def _build_message(event: str, args: dict) -> dict:
    repo = args.get("repository", {}).get("name", "?")

    if event == "push":
        ref = args.get("ref", "?").removeprefix("refs/heads/")
        commits = args.get("commits", [])
        n = len(commits)
        compare_url = args.get("compare", args.get("repository", {}).get("html_url", ""))
        lines = []
        for c in commits[-10:]:
            sha = c.get("id", "")[:7]
            msg = c.get("message", "").splitlines()[0]
            c_url = c.get("url", "")
            lines.append(f"[`{sha}`]({c_url}) {msg}")
        noun = "commit" if n == 1 else "commits"
        return {
            "embeds": [
                _embed(
                    _COLOR_GRAY,
                    f"[{repo}:{ref}] {n} new {noun}",
                    compare_url,
                    "\n".join(lines),
                    args,
                )
            ]
        }

    if event == "pull_request":
        action = args.get("action", "?")
        if action in _SKIPPED_ACTIONS:
            return {}
        pr = args.get("pull_request", {})
        n = pr.get("number", "?")
        title = pr.get("title", "?")
        url = pr.get("html_url", "")
        # The description is only news when the PR is opened, not on every later action
        body = (pr.get("body") or "")[:300] if action == "opened" else ""
        merged = pr.get("merged", False)
        if action == "closed":
            verb, color = ("merged", _COLOR_PURPLE) if merged else ("closed", _COLOR_RED)
        elif action in ("opened", "reopened"):
            verb, color = action, _COLOR_GREEN
        else:
            verb, color = action, _COLOR_BLUE
        return {
            "embeds": [
                _embed(
                    color,
                    f"[{repo}] Pull request {verb}: #{n} {title}",
                    url,
                    body,
                    args,
                )
            ]
        }

    if event == "pull_request_review":
        action = args.get("action", "?")
        if action != "submitted":
            return {}
        review = args.get("review", {})
        pr = args.get("pull_request", {})
        n = pr.get("number", "?")
        pr_title = pr.get("title", "?")
        state = review.get("state", "?").replace("_", " ").lower()
        url = review.get("html_url") or pr.get("html_url", "")
        body = (review.get("body") or "")[:300]
        color = _COLOR_GREEN if state == "approved" else (_COLOR_RED if state == "changes requested" else _COLOR_BLUE)
        return {
            "embeds": [
                _embed(
                    color,
                    f"[{repo}] Pull request review {state}: #{n} {pr_title}",
                    url,
                    body,
                    args,
                )
            ]
        }

    if event == "pull_request_review_comment":
        action = args.get("action", "?")
        if action != "created":
            return {}
        comment = args.get("comment", {})
        pr = args.get("pull_request", {})
        n = pr.get("number", "?")
        pr_title = pr.get("title", "?")
        url = comment.get("html_url", pr.get("html_url", ""))
        body = (comment.get("body") or "")[:300]
        return {
            "embeds": [
                _embed(
                    _COLOR_BLUE,
                    f"[{repo}] New review comment on pull request #{n}: {pr_title}",
                    url,
                    body,
                    args,
                )
            ]
        }

    if event == "issues":
        action = args.get("action", "?")
        if action in _SKIPPED_ACTIONS:
            return {}
        issue = args.get("issue", {})
        n = issue.get("number", "?")
        title = issue.get("title", "?")
        url = issue.get("html_url", "")
        # The description is only news when the issue is opened, not on every later action
        body = (issue.get("body") or "")[:300] if action == "opened" else ""
        if action == "opened":
            color = _COLOR_GREEN
        elif action == "closed":
            # Like GitHub: purple for completed (or no reason), gray for not planned / duplicate
            color = _COLOR_GRAY if issue.get("state_reason") in ("not_planned", "duplicate") else _COLOR_PURPLE
        else:
            color = _COLOR_BLUE
        embed = _embed(color, f"[{repo}] Issue {action}: #{n} {title}", url, body, args)
        if action == "opened":
            # Lets a later label change find this message and move it (see grouping.move)
            embed["footer"] = {"text": f"{_owner(args['repository'])}/{repo}#{n}"}
        return {"embeds": [embed]}

    if event == "issue_comment":
        action = args.get("action", "?")
        if action != "created":
            return {}
        issue = args.get("issue", {})
        comment = args.get("comment", {})
        n = issue.get("number", "?")
        title = issue.get("title", "?")
        url = comment.get("html_url", issue.get("html_url", ""))
        body = (comment.get("body") or "")[:300]
        kind = "pull request" if "pull_request" in issue else "issue"
        return {
            "embeds": [
                _embed(
                    _COLOR_BLUE,
                    f"[{repo}] New comment on {kind} #{n}: {title}",
                    url,
                    body,
                    args,
                )
            ]
        }

    if event == "create":
        ref_type = args.get("ref_type", "?")
        ref = args.get("ref", "?")
        url = args.get("repository", {}).get("html_url", "")
        return {
            "embeds": [
                _embed(
                    _COLOR_GREEN,
                    f"[{repo}] New {ref_type} created: {ref}",
                    url,
                    "",
                    args,
                )
            ]
        }

    if event == "delete":
        ref_type = args.get("ref_type", "?")
        ref = args.get("ref", "?")
        url = args.get("repository", {}).get("html_url", "")
        return {
            "embeds": [
                _embed(
                    _COLOR_RED,
                    f"[{repo}] {ref_type.capitalize()} deleted: {ref}",
                    url,
                    "",
                    args,
                )
            ]
        }

    if event == "release":
        action = args.get("action", "?")
        # "edited" updates the published message (see grouping.py)
        if action not in ("published", "edited"):
            return {}
        release = args.get("release", {})
        tag = release.get("tag_name", "?")
        name = release.get("name") or tag
        url = release.get("html_url", "")
        body = (release.get("body") or "")[:300]
        return {
            "embeds": [
                _embed(
                    _COLOR_GREEN,
                    f"[{repo}] New release published: {name}",
                    url,
                    body,
                    args,
                )
            ]
        }

    if event == "commit_comment":
        action = args.get("action", "?")
        if action != "created":
            return {}
        comment = args.get("comment", {})
        url = comment.get("html_url", "")
        body = (comment.get("body") or "")[:300]
        sha = comment.get("commit_id", "")[:7]
        return {
            "embeds": [
                _embed(
                    _COLOR_BLUE,
                    f"[{repo}] New comment on commit [`{sha}`]({url})",
                    url,
                    body,
                    args,
                )
            ]
        }

    if event == "fork":
        forkee = args.get("forkee", {})
        url = forkee.get("html_url", "")
        full_name = forkee.get("full_name", "?")
        return {"embeds": [_embed(_COLOR_GREEN, f"[{repo}] Fork created: {full_name}", url, "", args)]}

    if event == "watch":
        url = args.get("repository", {}).get("html_url", "")
        return {"embeds": [_embed(_COLOR_GRAY, f"[{repo}] New star", url, "", args)]}

    # Unhandled event — skip silently
    return {}


def _owner(repo: dict) -> str:
    return repo.get("owner", {}).get("login") or repo.get("full_name", "").partition("/")[0]


def _channel_ids(channel_names: list[str], channels_config: dict[str, str]) -> dict[str, str]:
    """Channel ID -> name. Unknown names fall back to 'default'; deduped so a channel never gets an event twice."""
    channel_ids: dict[str, str] = {}
    for channel_name in channel_names:
        channel_id = channels_config.get(channel_name) or channels_config.get("default", "")
        if channel_id:
            channel_ids.setdefault(channel_id, channel_name)
    return channel_ids


def _move_on_label_change(args: dict, labels: list[str], owner: str, repository: str, config: dict) -> str:
    """Move the PR's or issue's recent messages when adding/removing a label changes which channels it routes to.

    Lets someone who forgot the game label when creating the PR fix it afterwards. Only messages still
    inside the grouping window are moved; nothing is posted if the PR has none.
    """
    routing = _local("routing")
    grouping = _local("grouping")
    label = (args.get("label") or {}).get("name")
    if not label:
        return "label change without a label — skipped"
    with_label = {*labels, label}
    without_label = set(labels) - {label}
    after, before = (with_label, without_label) if args["action"] == "labeled" else (without_label, with_label)

    channels_config: dict[str, str] = config.get("discord", {}).get("channels", {})
    old_ids = _channel_ids(routing.resolve_channels(owner, repository, sorted(before), config), channels_config)
    new_ids = _channel_ids(routing.resolve_channels(owner, repository, sorted(after), config), channels_config)
    leaving = [i for i in old_ids if i not in new_ids]
    arriving = [i for i in new_ids if i not in old_ids]
    if not leaving and not arriving:
        return f"label '{label}' does not change the channels — skipped"

    client = grouping.DiscordClient()
    grouping.jitter()
    window = timedelta(minutes=config.get("webhook", {}).get("merge_window_minutes", 30))
    number = (args.get("pull_request") or args["issue"])["number"]
    footers = grouping.pr_footers(owner, repository, number)
    return grouping.move(client, leaving, arriving, footers, window)


def process(args: dict) -> dict:
    event = args.get("http", {}).get("headers", {}).get("x-github-event", "unknown")
    print(f"Received event: {event}")

    if "repository" not in args:
        return {"body": "unsupported request"}

    sender_login = args.get("sender", {}).get("login", "")
    if sender_login in _ignored_users:
        return {"body": "ignored user"}

    issue = args.get("issue") or args.get("pull_request")
    if issue:
        if issue.get("user", {}).get("login") in _ignored_users:
            return {"body": "ignored user"}
        labels = [lbl["name"] for lbl in issue.get("labels", [])]
    else:
        labels = []

    routing = _local("routing")
    config = routing.load_config()
    channels_config: dict[str, str] = config.get("discord", {}).get("channels", {})

    repo = args["repository"]
    repository = repo["name"]
    owner = _owner(repo)

    if event in ("pull_request", "issues") and args.get("action") in _SKIPPED_ACTIONS:
        return {"body": _move_on_label_change(args, labels, owner, repository, config)}

    payload = _build_message(event, args)
    if not payload:
        return {"body": f"event '{event}' produces no message — skipped"}

    if event == "push" and not routing.wants_push(owner, repository, config):
        return {"body": f"push events from {owner}/{repository} are not enabled — skipped"}
    comment_author = args.get("comment", {}).get("user", {}).get("login")
    if comment_author in config.get("webhook", {}).get("ignored_comment_authors", []):
        return {"body": f"comment by {comment_author} — skipped"}
    channel_names = routing.resolve_channels(owner, repository, labels, config)
    if not channel_names:
        return {"body": f"ignored repository {owner}/{repository}"}

    channel_ids = _channel_ids(channel_names, channels_config)
    if not channel_ids:
        return {"body": f"no channel ID configured for {channel_names} or 'default'"}

    # Bursts of related PR events (opened + review requests, approve → queue → merge, ...) edit one message
    grouping = _local("grouping")
    update = grouping.build_update(event, args, owner, _author(args))
    client = None
    if update:
        client = grouping.DiscordClient()
        grouping.jitter()
    window = timedelta(minutes=config.get("webhook", {}).get("merge_window_minutes", 30))

    results = []
    for channel_id, channel_name in channel_ids.items():
        print(f"Routing {event} from {owner}/{repository} to channel {channel_name} ({channel_id})")
        if update:
            results.append(grouping.deliver(client, channel_id, update, window))
        else:
            results.append(_post_to_discord(channel_id, payload))
    return {"body": "\n".join(results)}


def main(args: dict, context=None) -> dict:
    try:
        return process(args)
    except Exception as e:
        # Non-200 so the failure (and its traceback) shows up in the GitHub App's Recent Deliveries
        return {"statusCode": 500, "body": "".join(traceback.format_exception(e))}
