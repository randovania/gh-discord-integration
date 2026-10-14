import os
import sys
import traceback
from pathlib import Path

DISCORD_API = "https://discord.com/api/v10"

_ignored_users = {
    "codecov[bot]",
    "dependabot[bot]",
    "pre-commit-ci[bot]",
    "renovate[bot]",
}

# Colors matching Discord's GitHub integration
_COLOR_GREEN = 0x2ECC71  # opened / created
_COLOR_RED = 0xCB2431  # closed / deleted
_COLOR_PURPLE = 0x6F42C1  # merged
_COLOR_GRAY = 0x24292E  # push
_COLOR_BLUE = 0x0075CA  # comment / review


def _routing():
    # Imported lazily so a packaging problem fails inside main()'s try (visible in GitHub's
    # Recent Deliveries) instead of killing the function at load time with no logs.
    here = str(Path(__file__).resolve().parent) if "__file__" in globals() else str(Path.cwd())
    if here not in sys.path:
        sys.path.insert(0, here)
    import routing

    return routing


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
        issue = args.get("issue", {})
        n = issue.get("number", "?")
        title = issue.get("title", "?")
        url = issue.get("html_url", "")
        # The description is only news when the issue is opened, not on every later action
        body = (issue.get("body") or "")[:300] if action == "opened" else ""
        color = _COLOR_GREEN if action == "opened" else (_COLOR_RED if action == "closed" else _COLOR_BLUE)
        return {"embeds": [_embed(color, f"[{repo}] Issue {action}: #{n} {title}", url, body, args)]}

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
        if action != "published":
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

    payload = _build_message(event, args)
    if not payload:
        return {"body": f"event '{event}' produces no message — skipped"}

    routing = _routing()
    config = routing.load_config()
    channels_config: dict[str, str] = config.get("discord", {}).get("channels", {})

    repo = args["repository"]
    repository = repo["name"]
    owner = repo.get("owner", {}).get("login") or repo.get("full_name", "").partition("/")[0]
    channel_names = routing.resolve_channels(owner, repository, labels, config)
    if not channel_names:
        return {"body": f"ignored repository {owner}/{repository}"}

    # Unknown channel names fall back to 'default'; dedupe so a channel never gets the same event twice
    channel_ids: dict[str, str] = {}
    for channel_name in channel_names:
        channel_id = channels_config.get(channel_name) or channels_config.get("default", "")
        if channel_id:
            channel_ids.setdefault(channel_id, channel_name)

    if not channel_ids:
        return {"body": f"no channel ID configured for {channel_names} or 'default'"}

    results = []
    for channel_id, channel_name in channel_ids.items():
        print(f"Routing {event} from {owner}/{repository} to channel {channel_name} ({channel_id})")
        results.append(_post_to_discord(channel_id, payload))
    return {"body": "\n".join(results)}


def main(args: dict, context=None) -> dict:
    try:
        return process(args)
    except Exception as e:
        # Non-200 so the failure (and its traceback) shows up in the GitHub App's Recent Deliveries
        return {"statusCode": 500, "body": "".join(traceback.format_exception(e))}
