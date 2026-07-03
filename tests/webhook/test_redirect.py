import json
from pathlib import Path
from unittest.mock import patch

import pytest

import redirect

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
DISCORD_CHANNEL_ID = "111222333444555666"

TEST_CONFIG = {
    "discord": {"channels": {"default": DISCORD_CHANNEL_ID}},
    "repo_channels": {},
    "label_channels": {},
}


def load_webhook(name: str) -> dict:
    return json.loads((FIXTURES_DIR / "webhooks" / name).read_text())


def load_discord_response(name: str) -> dict:
    return json.loads((FIXTURES_DIR / "discord" / name).read_text())


class TestBuildMessage:
    def test_pull_request_opened(self):
        args = load_webhook("pull_request_opened.json")
        msg = redirect._build_message("pull_request", args)
        embed = msg["embeds"][0]
        assert embed["color"] == redirect._COLOR_GREEN
        assert "opened" in embed["title"]
        assert "#42" in embed["title"]
        assert "test-repo" in embed["title"]

    def test_pull_request_merged(self):
        args = load_webhook("pull_request_merged.json")
        msg = redirect._build_message("pull_request", args)
        embed = msg["embeds"][0]
        assert embed["color"] == redirect._COLOR_PURPLE
        assert "merged" in embed["title"]
        assert "#43" in embed["title"]

    def test_pull_request_closed_without_merge(self):
        args = load_webhook("pull_request_merged.json")
        args = {**args, "pull_request": {**args["pull_request"], "merged": False}}
        msg = redirect._build_message("pull_request", args)
        embed = msg["embeds"][0]
        assert embed["color"] == redirect._COLOR_RED
        assert "closed" in embed["title"]

    def test_pull_request_body_truncated_at_300(self):
        args = load_webhook("pull_request_opened.json")
        args = {**args, "pull_request": {**args["pull_request"], "body": "x" * 500}}
        msg = redirect._build_message("pull_request", args)
        assert len(msg["embeds"][0]["description"]) == 300

    def test_push_single_commit(self):
        args = load_webhook("push.json")
        msg = redirect._build_message("push", args)
        embed = msg["embeds"][0]
        assert embed["color"] == redirect._COLOR_GRAY
        assert "main" in embed["title"]
        assert "1 new commit" in embed["title"]
        assert "def456a" in embed["description"]

    def test_push_strips_refs_heads_prefix(self):
        args = {**load_webhook("push.json"), "ref": "refs/heads/feature-branch"}
        msg = redirect._build_message("push", args)
        assert "feature-branch" in msg["embeds"][0]["title"]
        assert "refs/heads" not in msg["embeds"][0]["title"]

    def test_push_plural_commits(self):
        args = load_webhook("push.json")
        args = {**args, "commits": args["commits"] * 3}
        msg = redirect._build_message("push", args)
        assert "3 new commits" in msg["embeds"][0]["title"]

    def test_pull_request_review_approved(self):
        args = load_webhook("pull_request_review_approved.json")
        msg = redirect._build_message("pull_request_review", args)
        embed = msg["embeds"][0]
        assert embed["color"] == redirect._COLOR_GREEN
        assert "approved" in embed["title"]
        assert "#42" in embed["title"]

    def test_pull_request_review_changes_requested(self):
        args = load_webhook("pull_request_review_approved.json")
        args = {**args, "review": {**args["review"], "state": "changes_requested"}}
        msg = redirect._build_message("pull_request_review", args)
        embed = msg["embeds"][0]
        assert embed["color"] == redirect._COLOR_RED
        assert "changes requested" in embed["title"]

    def test_pull_request_review_not_submitted_is_skipped(self):
        args = {**load_webhook("pull_request_review_approved.json"), "action": "dismissed"}
        msg = redirect._build_message("pull_request_review", args)
        assert msg == {}

    def test_unknown_event_returns_empty(self):
        msg = redirect._build_message("deployment", {"repository": {"name": "repo"}})
        assert msg == {}

    def test_embed_author_uses_sender(self):
        args = load_webhook("pull_request_opened.json")
        msg = redirect._build_message("pull_request", args)
        author = msg["embeds"][0]["author"]
        assert author["name"] == "octocat"
        assert "octocat" in author["url"]


class TestResolveChannel:
    def test_label_takes_priority_over_repo(self):
        config = {
            "label_channels": {"bug": "bugs-channel"},
            "repo_channels": {"test-repo": "repo-channel"},
        }
        result = redirect._resolve_channel("test-repo", ["bug"], config)
        assert result == "bugs-channel"

    def test_repo_fallback_when_no_label_match(self):
        config = {
            "label_channels": {"bug": "bugs-channel"},
            "repo_channels": {"test-repo": "repo-channel"},
        }
        result = redirect._resolve_channel("test-repo", ["enhancement"], config)
        assert result == "repo-channel"

    def test_default_when_nothing_matches(self):
        config = {"label_channels": {}, "repo_channels": {}}
        result = redirect._resolve_channel("unknown-repo", [], config)
        assert result == "default"

    def test_first_matching_label_wins(self):
        config = {
            "label_channels": {"bug": "bugs-channel", "urgent": "urgent-channel"},
            "repo_channels": {},
        }
        result = redirect._resolve_channel("repo", ["urgent", "bug"], config)
        assert result == "urgent-channel"


class TestProcess:
    def test_posts_embed_to_discord(self, httpx_mock, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        httpx_mock.add_response(
            url=f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            json=load_discord_response("message_ok.json"),
            status_code=200,
        )
        args = load_webhook("pull_request_opened.json")
        with patch("redirect._load_config", return_value=TEST_CONFIG):
            result = redirect.process(args)
        assert "Posted to channel" in result["body"]
        assert DISCORD_CHANNEL_ID in result["body"]

    def test_ignores_bot_sender(self):
        args = {
            **load_webhook("pull_request_opened.json"),
            "sender": {"login": "dependabot[bot]", "avatar_url": ""},
        }
        result = redirect.process(args)
        assert result == {"body": "ignored user"}

    def test_ignores_pr_authored_by_bot(self, monkeypatch):
        args = load_webhook("pull_request_opened.json")
        args = {
            **args,
            "pull_request": {
                **args["pull_request"],
                "user": {"login": "renovate[bot]"},
                "labels": [],
            },
        }
        result = redirect.process(args)
        assert result == {"body": "ignored user"}

    def test_no_repository_returns_unsupported(self):
        args = {"http": {"headers": {"x-github-event": "ping"}}}
        result = redirect.process(args)
        assert result == {"body": "unsupported request"}

    def test_unknown_event_skipped(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        args = {
            **load_webhook("pull_request_opened.json"),
            "http": {"headers": {"x-github-event": "deployment"}},
        }
        with patch("redirect._load_config", return_value=TEST_CONFIG):
            result = redirect.process(args)
        assert "skipped" in result["body"]

    def test_no_channel_configured_returns_error(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        empty_config = {
            "discord": {"channels": {}},
            "repo_channels": {},
            "label_channels": {},
        }
        args = load_webhook("pull_request_opened.json")
        with patch("redirect._load_config", return_value=empty_config):
            result = redirect.process(args)
        assert "no channel ID configured" in result["body"]
