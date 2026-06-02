import github
import json
import os
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from github import Github

CACHE_FILE = Path("pr_cache.json")
CACHE_TTL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def _load_cache() -> list[dict] | None:
    if not CACHE_FILE.exists():
        return None
    data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    cached_at = datetime.fromisoformat(data["cached_at"])
    age = (datetime.now(timezone.utc) - cached_at).total_seconds()
    if age >= CACHE_TTL_SECONDS:
        return None
    print(f"Using cached PR data (age: {age:.0f}s).")
    return data["prs"]


def _save_cache(prs: list[dict]) -> None:
    CACHE_FILE.write_text(
        json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(), "prs": prs}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# GitHub fetching
# ---------------------------------------------------------------------------

def _collapse_reviews(raw_reviews) -> list[dict]:
    """Return the latest meaningful review per reviewer, excluding dismissed/pending."""
    by_reviewer: dict[str, dict] = {}
    for review in sorted(raw_reviews, key=lambda r: r.submitted_at or datetime.min.replace(tzinfo=timezone.utc)):
        if review.state not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            continue
        if review.user is None:
            continue
        by_reviewer[review.user.login] = {
            "reviewer": review.user.login,
            "state": review.state,
            "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
        }
    return list(by_reviewer.values())


def fetch_prs(token: str, org_name: str, *, refresh: bool = False, skip_repos: set[str] | None = None) -> list[dict]:
    if not refresh:
        cached = _load_cache()
        if cached is not None:
            if skip_repos:
                cached = [pr for pr in cached if pr["repo"] not in skip_repos]
            return cached

    print(f"Fetching open PRs from {org_name}…")
    g = Github(auth=github.Auth.Token(token))
    org = g.get_organization(org_name)

    prs: list[dict] = []
    for repo in org.get_repos():
        if repo.archived or repo.fork:
            continue
        if skip_repos and repo.name in skip_repos:
            print(f"  {repo.name}: skipped")
            continue
        open_prs = list(repo.get_pulls(state="open"))
        non_draft = [pr for pr in open_prs if not pr.draft]
        if not non_draft:
            continue
        print(f"  {repo.name}: {len(non_draft)} open PR(s)")
        for pr in non_draft:
            req_users, req_teams = pr.get_review_requests()
            reviews = _collapse_reviews(pr.get_reviews())
            prs.append({
                "repo": repo.name,
                "number": pr.number,
                "title": pr.title,
                "url": pr.html_url,
                "author": pr.user.login,
                "labels": [lbl.name for lbl in pr.labels],
                "created_at": pr.created_at.isoformat(),
                "updated_at": pr.updated_at.isoformat(),
                "requested_reviewers": [u.login for u in req_users],
                "requested_teams": [t.slug for t in req_teams],
                "reviews": reviews,
            })

    _save_cache(prs)
    print(f"Fetched {len(prs)} PR(s), cached to {CACHE_FILE}.")
    return prs


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_pr(pr: dict, dependency_authors: set[str]) -> dict:
    updated_at = datetime.fromisoformat(pr["updated_at"])
    age_days = (datetime.now(timezone.utc) - updated_at).days

    if pr["author"] in dependency_authors:
        return {**pr, "attention": ["dependencies"], "age_days": age_days}

    reviews = pr["reviews"]
    requested_reviewers = pr["requested_reviewers"]
    requested_teams = pr["requested_teams"]

    # Only APPROVED / CHANGES_REQUESTED are decisive; COMMENTED is informational.
    decisive = [r for r in reviews if r["state"] in ("APPROVED", "CHANGES_REQUESTED")]
    has_changes_requested = any(r["state"] == "CHANGES_REQUESTED" for r in decisive)
    has_approved = any(r["state"] == "APPROVED" for r in decisive)
    has_pending_request = bool(requested_reviewers or requested_teams)
    has_any_reviewer_activity = bool(reviews or requested_reviewers or requested_teams)

    attention: list[str] = []
    if not has_any_reviewer_activity:
        attention.append("needs_reviewer")
    if has_changes_requested:
        attention.append("changes_requested")
    if has_pending_request and not has_changes_requested:
        attention.append("awaiting_review")
    if has_approved and not has_pending_request and not has_changes_requested:
        attention.append("ready_to_merge")

    return {**pr, "attention": attention, "age_days": age_days}


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------

def resolve_channel(pr: dict, config: dict) -> str:
    """Label mapping beats repo mapping; fall back to 'default'."""
    label_channels: dict[str, str] = config.get("label_channels", {})
    repo_channels: dict[str, str] = config.get("repo_channels", {})

    for label in pr["labels"]:
        if label in label_channels:
            return label_channels[label]

    return repo_channels.get(pr["repo"], "default")


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

_ATTENTION_EMOJI = {
    "needs_reviewer": "⚠️",
    "changes_requested": "🔴",
    "awaiting_review": "👀",
    "ready_to_merge": "✅",
    "dependencies": "🤖",
}

_ATTENTION_HEADING = {
    "needs_reviewer": "Needs a reviewer",
    "changes_requested": "Changes requested — author action needed",
    "awaiting_review": "Awaiting review",
    "ready_to_merge": "Ready to merge",
    "dependencies": "Dependency updates",
}

_ATTENTION_ORDER = ["needs_reviewer", "changes_requested", "awaiting_review", "ready_to_merge", "dependencies"]


def _age_str(days: int) -> str:
    if days == 0:
        return "active today"
    return f"inactive {days}d"


def _pr_line(pr: dict) -> str:
    age = _age_str(pr["age_days"])
    return f"• [{pr['repo']}#{pr['number']}](<{pr['url']}>) **{pr['title']}** — {age}"


def build_reports(prs: list[dict], config: dict, dependency_authors: set[str]) -> dict[str, str]:
    """Return a mapping of channel_name → formatted Discord message content."""
    # Group classified PRs by channel, then by attention category.
    channel_buckets: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for pr in prs:
        classified = classify_pr(pr, dependency_authors)
        channel = resolve_channel(classified, config)
        for att in classified["attention"]:
            channel_buckets[channel][att].append(classified)
        if not classified["attention"]:
            channel_buckets[channel]["_other"].append(classified)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    reports: dict[str, str] = {}

    for channel, buckets in channel_buckets.items():
        sections: list[str] = [f"## PR Health Report — {now_str}"]
        total = sum(len(v) for v in buckets.values())
        sections.append(f"*{total} open PR(s) across the org*\n")

        for att in _ATTENTION_ORDER:
            if att not in buckets:
                continue
            heading = f"{_ATTENTION_EMOJI[att]} **{_ATTENTION_HEADING[att]}**"
            lines = [heading]
            for pr in sorted(buckets[att], key=lambda p: p["age_days"], reverse=True):
                lines.append(_pr_line(pr))
            sections.append("\n".join(lines))

        if "_other" in buckets:
            lines = ["📌 **Other open PRs**"]
            for pr in sorted(buckets["_other"], key=lambda p: p["age_days"], reverse=True):
                lines.append(_pr_line(pr))
            sections.append("\n".join(lines))

        reports[channel] = "\n\n".join(sections)

    return reports


# ---------------------------------------------------------------------------
# Discord posting
# ---------------------------------------------------------------------------

def post_to_discord(webhook_url: str, content: str) -> None:
    # Discord message content limit is 2000 characters; split naively on newlines.
    chunks: list[str] = []
    current = ""
    for line in content.splitlines(keepends=True):
        if len(current) + len(line) > 1900:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    with httpx.Client() as client:
        for chunk in chunks:
            resp = client.post(webhook_url, json={"content": chunk})
            resp.raise_for_status()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args
    refresh = "--refresh" in args

    config_path = Path("config.toml")
    if not config_path.exists():
        print("config.toml not found.", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    github_config = config["github"]
    org_name: str = github_config["org"]
    skip_repos: set[str] = set(github_config.get("skip_repos", []))
    dependency_authors: set[str] = set(github_config.get("dependency_authors", [
        "dependabot[bot]", "renovate[bot]", "pre-commit-ci[bot]",
    ]))

    prs = fetch_prs(token, org_name, refresh=refresh, skip_repos=skip_repos)

    if not prs:
        print("No open non-draft PRs found.")
        return

    reports = build_reports(prs, config, dependency_authors)
    channels_config: dict[str, str] = config.get("discord", {}).get("channels", {})

    for channel_name, content in reports.items():
        webhook_url = channels_config.get(channel_name, "")

        if dry_run:
            print(f"\n{'=' * 60}")
            print(f"Channel: {channel_name}")
            print("=" * 60)
            print(content)
            continue

        if not webhook_url:
            print(f"[{channel_name}] No webhook URL configured — skipping. (Use --dry-run to preview.)")
            continue

        print(f"Posting to channel '{channel_name}'…")
        post_to_discord(webhook_url, content)
        print(f"  Done.")


if __name__ == "__main__":
    main()
