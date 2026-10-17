import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
REDIRECT_DIR = Path(__file__).parent.parent.parent / "webhook" / "packages" / "ghapp" / "redirect"

# DO needs the handler to be called __main__.py, so load it by path under a usable name
_spec = importlib.util.spec_from_file_location("redirect", REDIRECT_DIR / "__main__.py")
redirect = importlib.util.module_from_spec(_spec)
sys.modules["redirect"] = redirect
_spec.loader.exec_module(redirect)
DISCORD_CHANNEL_ID = "111222333444555666"

TEST_CONFIG = {
    "github": {"org": "randovania"},
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

    def test_pull_request_merged_omits_description(self):
        args = load_webhook("pull_request_merged.json")
        embed = redirect._build_message("pull_request", args)["embeds"][0]
        assert "description" not in embed

    @pytest.mark.parametrize("event", ["issues", "pull_request"])
    @pytest.mark.parametrize("action", ["labeled", "unlabeled"])
    def test_label_changes_are_skipped(self, event, action):
        args = {**load_webhook("pull_request_opened.json"), "action": action, "issue": {"number": 1}}
        assert redirect._build_message(event, args) == {}

    def test_issue_opened_includes_description(self):
        args = {**load_webhook("pull_request_opened.json"), "action": "opened", "issue": {"number": 1, "body": "Hi!"}}
        embed = redirect._build_message("issues", args)["embeds"][0]
        assert embed["description"] == "Hi!"

    def test_issue_closed_omits_description(self):
        args = {**load_webhook("pull_request_opened.json"), "action": "closed", "issue": {"number": 1, "body": "Hi!"}}
        embed = redirect._build_message("issues", args)["embeds"][0]
        assert embed["color"] == redirect._COLOR_RED
        assert "description" not in embed

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
        args = {
            **load_webhook("pull_request_review_approved.json"),
            "action": "dismissed",
        }
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


class TestProcess:
    def test_posts_embed_to_discord(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        response = MagicMock(status_code=200)
        response.json.return_value = load_discord_response("message_ok.json")
        # An ungrouped event goes straight to a new message
        args = {
            **load_webhook("pull_request_opened.json"),
            "http": {"headers": {"x-github-event": "issues"}},
            "issue": {"number": 7, "title": "Bug", "labels": []},
        }
        with (
            patch("routing.load_config", return_value=TEST_CONFIG),
            patch("requests.post", return_value=response) as post,
        ):
            result = redirect.process(args)
        assert post.call_args.args[0] == f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
        assert post.call_args.kwargs["headers"] == {"Authorization": "Bot test-bot-token"}
        assert "Posted to channel" in result["body"]
        assert DISCORD_CHANNEL_ID in result["body"]

    def test_grouped_event_goes_through_grouping(self, discord):
        args = load_webhook("pull_request_opened.json")
        with patch("routing.load_config", return_value=TEST_CONFIG):
            result = redirect.process(args)
        [embed] = discord.embeds(DISCORD_CHANNEL_ID)
        assert embed["footer"]["text"] == "randovania/test-repo#42"
        assert "Posted new message" in result["body"]

    def test_grouped_event_jitters_once_for_all_channels(self, discord, monkeypatch):
        sleeps = []
        monkeypatch.setattr("grouping._sleep", sleeps.append)
        config = {
            "github": {"org": "randovania"},
            "discord": {"channels": {"default": DISCORD_CHANNEL_ID, "a": "111", "b": "222"}},
            "repo_channels": {"test-repo": {"channels": ["a", "b"]}},
            "label_channels": {},
        }
        with patch("routing.load_config", return_value=config):
            redirect.process(load_webhook("pull_request_opened.json"))
        assert len(sleeps) == 1
        assert sleeps[0] <= 1.5

    def test_posts_to_every_resolved_channel_once(self, discord, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        config = {
            "github": {"org": "randovania"},
            "discord": {"channels": {"default": DISCORD_CHANNEL_ID, "a": "111", "b": "222"}},
            # 'missing' has no ID so it falls back to default
            "repo_channels": {"other/test-repo": {"channels": ["a", "b", "missing", "a"]}},
            "label_channels": {},
        }
        args = load_webhook("pull_request_opened.json")
        args["repository"] = {**args["repository"], "full_name": "other/test-repo", "owner": {"login": "other"}}
        with patch("routing.load_config", return_value=config):
            redirect.process(args)
        assert list(discord.channels) == ["111", "222", DISCORD_CHANNEL_ID]
        assert all(len(messages) == 1 for messages in discord.channels.values())

    @pytest.mark.parametrize("event", ["issue_comment", "pull_request_review_comment", "commit_comment"])
    def test_comment_by_ignored_author_dropped(self, event):
        args = {
            **load_webhook("pull_request_opened.json"),
            "http": {"headers": {"x-github-event": event}},
            "action": "created",
            "issue": {"number": 9663, "title": "Upgrade to PySide 6.12", "pull_request": {}, "labels": []},
            "comment": {"user": {"login": "github-actions[bot]"}, "body": "# uv Lockfile Report"},
            "sender": {"login": "github-actions[bot]"},
        }
        config = {**TEST_CONFIG, "webhook": {"ignored_comment_authors": ["github-actions[bot]"]}}
        with (
            patch("routing.load_config", return_value=config),
            patch("requests.post") as post,
        ):
            result = redirect.process(args)
        assert result == {"body": "comment by github-actions[bot] — skipped"}
        post.assert_not_called()

    def test_bundled_config_ignores_actions_comments(self):
        assert "github-actions[bot]" in redirect._local("routing").load_config()["webhook"]["ignored_comment_authors"]

    def test_push_dropped_unless_enabled(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        args = load_webhook("push.json")
        with (
            patch("routing.load_config", return_value=TEST_CONFIG),
            patch("requests.post") as post,
        ):
            result = redirect.process(args)
        assert "not enabled" in result["body"]
        post.assert_not_called()

    def test_push_posted_when_enabled(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        args = load_webhook("push.json")
        config = {**TEST_CONFIG, "webhook": {"push_repos": ["randovania/test-repo"]}}
        with (
            patch("routing.load_config", return_value=config),
            patch("requests.post", return_value=MagicMock(status_code=200)) as post,
        ):
            redirect.process(args)
        post.assert_called_once()

    def test_main_returns_500_on_error(self):
        args = load_webhook("pull_request_opened.json")
        with patch(
            "routing.load_config",
            side_effect=FileNotFoundError("config.toml not found"),
        ):
            result = redirect.main(args)
        assert result["statusCode"] == 500
        assert "config.toml not found" in result["body"]

    def test_ignores_other_org(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        args = load_webhook("pull_request_opened.json")
        args["repository"] = {**args["repository"], "full_name": "someone/test-repo", "owner": {"login": "someone"}}
        with (
            patch("routing.load_config", return_value=TEST_CONFIG),
            patch("requests.post") as post,
        ):
            result = redirect.process(args)
        assert result == {"body": "ignored repository someone/test-repo"}
        post.assert_not_called()

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
        with patch("routing.load_config", return_value=TEST_CONFIG):
            result = redirect.process(args)
        assert "skipped" in result["body"]

    def test_no_channel_configured_returns_error(self, monkeypatch):
        monkeypatch.setenv("DISCORD_TOKEN", "test-bot-token")
        empty_config = {
            "github": {"org": "randovania"},
            "discord": {"channels": {}},
            "repo_channels": {},
            "label_channels": {},
        }
        args = load_webhook("pull_request_opened.json")
        with patch("routing.load_config", return_value=empty_config):
            result = redirect.process(args)
        assert "no channel ID configured" in result["body"]
