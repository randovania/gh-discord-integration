import os
import tomllib
import traceback
from pathlib import Path

import httpx

DISCORD_API = "https://discord.com/api/v10"

_ignored_users = {
    "codecov[bot]",
    "dependabot[bot]",
    "pre-commit-ci[bot]",
    "renovate[bot]",
}

# Colors matching Discord's GitHub integration
_COLOR_GREEN = 0x2ECC71   # opened / created
_COLOR_RED = 0xCB2431     # closed / deleted
_COLOR_PURPLE = 0x6F42C1  # merged
_COLOR_GRAY = 0x24292E    # push
_COLOR_BLUE = 0x0075CA    # comment / review


def _load_config() -> dict:
    # Walk up from this file's directory until config.toml is found.
    # In the DO container the deployed root is a few levels up from the function dir.
    here = Path(__file__).resolve().parent
    for directory in [here, *here.parents]:
        candidate = directory / "config.toml"
        if candidate.exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f)
    raise FileNotFoundError("config.toml not found")


def _resolve_channel(repository: str, labels: list[str], config: dict) -> str:
    label_channels: dict[str, str] = config.get("label_channels", {})
    repo_channels: dict[str, str] = config.get("repo_channels", {})
    for label in labels:
        if label in label_channels:
            return label_channels[label]
    return repo_channels.get(repository, "default")


def _post_to_discord(channel_id: str, payload: dict) -> str:
    token = os.environ["DISCORD_TOKEN"]
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    with httpx.Client(headers={"Authorization": f"Bot {token}"}) as client:
        resp = client.post(url, json=payload)
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
        return {"embeds": [_embed(
            _COLOR_GRAY,
            f"[{repo}:{ref}] {n} new {noun}",
            compare_url,
            "\n".join(lines),
            args,
        )]}

    if event == "pull_request":
        action = args.get("action", "?")
        pr = args.get("pull_request", {})
        n = pr.get("number", "?")
        title = pr.get("title", "?")
        url = pr.get("html_url", "")
        body = (pr.get("body") or "")[:300]
        merged = pr.get("merged", False)
        if action == "closed":
            verb, color = ("merged", _COLOR_PURPLE) if merged else ("closed", _COLOR_RED)
        elif action in ("opened", "reopened"):
            verb, color = action, _COLOR_GREEN
        else:
            verb, color = action, _COLOR_BLUE
        return {"embeds": [_embed(color, f"[{repo}] Pull request {verb}: #{n} {title}", url, body, args)]}

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
        return {"embeds": [_embed(color, f"[{repo}] Pull request review {state}: #{n} {pr_title}", url, body, args)]}

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
        return {"embeds": [_embed(_COLOR_BLUE, f"[{repo}] New review comment on pull request #{n}: {pr_title}", url, body, args)]}

    if event == "issues":
        action = args.get("action", "?")
        issue = args.get("issue", {})
        n = issue.get("number", "?")
        title = issue.get("title", "?")
        url = issue.get("html_url", "")
        body = (issue.get("body") or "")[:300]
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
        return {"embeds": [_embed(_COLOR_BLUE, f"[{repo}] New comment on {kind} #{n}: {title}", url, body, args)]}

    if event == "create":
        ref_type = args.get("ref_type", "?")
        ref = args.get("ref", "?")
        url = args.get("repository", {}).get("html_url", "")
        return {"embeds": [_embed(_COLOR_GREEN, f"[{repo}] New {ref_type} created: {ref}", url, "", args)]}

    if event == "delete":
        ref_type = args.get("ref_type", "?")
        ref = args.get("ref", "?")
        url = args.get("repository", {}).get("html_url", "")
        return {"embeds": [_embed(_COLOR_RED, f"[{repo}] {ref_type.capitalize()} deleted: {ref}", url, "", args)]}

    if event == "release":
        action = args.get("action", "?")
        if action != "published":
            return {}
        release = args.get("release", {})
        tag = release.get("tag_name", "?")
        name = release.get("name") or tag
        url = release.get("html_url", "")
        body = (release.get("body") or "")[:300]
        return {"embeds": [_embed(_COLOR_GREEN, f"[{repo}] New release published: {name}", url, body, args)]}

    if event == "commit_comment":
        action = args.get("action", "?")
        if action != "created":
            return {}
        comment = args.get("comment", {})
        url = comment.get("html_url", "")
        body = (comment.get("body") or "")[:300]
        sha = comment.get("commit_id", "")[:7]
        return {"embeds": [_embed(_COLOR_BLUE, f"[{repo}] New comment on commit [`{sha}`]({url})", url, body, args)]}

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

    config = _load_config()
    channels_config: dict[str, str] = config.get("discord", {}).get("channels", {})

    repository = args["repository"]["name"]
    channel_name = _resolve_channel(repository, labels, config)
    channel_id = channels_config.get(channel_name) or channels_config.get("default", "")

    if not channel_id:
        return {"body": f"no channel ID configured for '{channel_name}' or 'default'"}

    print(f"Routing {event} from {repository} to channel {channel_name} ({channel_id})")

    result = _post_to_discord(channel_id, payload)
    return {"body": result}


def main(args: dict) -> dict:
    try:
        return process(args)
    except Exception as e:
        return {"body": "".join(traceback.format_exception(e))}

if __name__ == "__main__":
    import json
    import sys

    args = json.load(sys.stdin)
    result = process(args)
    print(json.dumps(result, indent=2))