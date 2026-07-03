from datetime import datetime, timezone, timedelta

import httpx
import pytest

import main

DISCORD_CHANNEL_ID = "999888777666555444"


def make_pr(**overrides) -> dict:
    base = {
        "repo": "test-repo",
        "number": 1,
        "title": "Test PR",
        "url": "https://github.com/org/test-repo/pull/1",
        "author": "alice",
        "labels": [],
        "created_at": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        "updated_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "requested_reviewers": [],
        "requested_teams": [],
        "reviews": [],
    }
    return {**base, **overrides}


class TestClassifyPR:
    def test_needs_reviewer_when_no_activity(self):
        pr = make_pr()
        result = main.classify_pr(pr, set())
        assert "needs_reviewer" in result["attention"]

    def test_awaiting_review_when_reviewer_requested(self):
        pr = make_pr(requested_reviewers=["bob"])
        result = main.classify_pr(pr, set())
        assert "awaiting_review" in result["attention"]
        assert "needs_reviewer" not in result["attention"]

    def test_awaiting_review_when_team_requested(self):
        pr = make_pr(requested_teams=["core-team"])
        result = main.classify_pr(pr, set())
        assert "awaiting_review" in result["attention"]

    def test_changes_requested(self):
        pr = make_pr(reviews=[{"reviewer": "bob", "state": "CHANGES_REQUESTED", "submitted_at": None}])
        result = main.classify_pr(pr, set())
        assert "changes_requested" in result["attention"]

    def test_changes_requested_suppresses_awaiting_review(self):
        pr = make_pr(
            requested_reviewers=["carol"],
            reviews=[{"reviewer": "bob", "state": "CHANGES_REQUESTED", "submitted_at": None}],
        )
        result = main.classify_pr(pr, set())
        assert "changes_requested" in result["attention"]
        assert "awaiting_review" not in result["attention"]

    def test_ready_to_merge_when_approved_and_no_pending_request(self):
        pr = make_pr(reviews=[{"reviewer": "bob", "state": "APPROVED", "submitted_at": None}])
        result = main.classify_pr(pr, set())
        assert "ready_to_merge" in result["attention"]

    def test_not_ready_to_merge_when_reviewer_still_pending(self):
        pr = make_pr(
            requested_reviewers=["carol"],
            reviews=[{"reviewer": "bob", "state": "APPROVED", "submitted_at": None}],
        )
        result = main.classify_pr(pr, set())
        assert "ready_to_merge" not in result["attention"]
        assert "awaiting_review" in result["attention"]

    def test_dependency_author_overrides_all(self):
        pr = make_pr(
            author="dependabot[bot]",
            reviews=[{"reviewer": "bob", "state": "APPROVED", "submitted_at": None}],
        )
        result = main.classify_pr(pr, {"dependabot[bot]"})
        assert result["attention"] == ["dependencies"]

    def test_commented_review_does_not_count_as_decisive(self):
        pr = make_pr(reviews=[{"reviewer": "bob", "state": "COMMENTED", "submitted_at": None}])
        result = main.classify_pr(pr, set())
        assert "needs_reviewer" not in result["attention"]
        assert "ready_to_merge" not in result["attention"]

    def test_age_days_computed_from_updated_at(self):
        pr = make_pr(updated_at=(datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        result = main.classify_pr(pr, set())
        assert result["age_days"] == 7


class TestResolveChannel:
    CONFIG = {
        "label_channels": {"bug": "bugs-channel"},
        "repo_channels": {"special-repo": "special-channel"},
    }

    def test_label_beats_repo(self):
        pr = make_pr(repo="special-repo", labels=["bug"])
        assert main.resolve_channel(pr, self.CONFIG) == "bugs-channel"

    def test_repo_match(self):
        pr = make_pr(repo="special-repo", labels=[])
        assert main.resolve_channel(pr, self.CONFIG) == "special-channel"

    def test_default_when_nothing_matches(self):
        pr = make_pr(repo="unknown-repo", labels=[])
        assert main.resolve_channel(pr, self.CONFIG) == "default"


class TestSplitMessage:
    def test_short_content_returned_as_single_chunk(self):
        chunks = main._split_message("short message")
        assert chunks == ["short message"]

    def test_long_content_split_into_chunks(self):
        line = "x" * 1000 + "\n"
        content = line * 3
        chunks = main._split_message(content)
        assert len(chunks) > 1

    def test_each_chunk_within_1900_char_limit(self):
        content = "\n".join(["word"] * 500)
        for chunk in main._split_message(content):
            assert len(chunk) <= 1900

    def test_no_content_lost(self):
        content = "\n".join(f"line {i}" for i in range(200))
        chunks = main._split_message(content)
        assert "".join(chunks) == content


class TestPostToChannel:
    def test_posts_single_message_for_short_content(self, httpx_mock):
        httpx_mock.add_response(
            url=f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            json={"id": "1"},
            status_code=200,
        )
        with httpx.Client() as client:
            main.post_to_channel(client, DISCORD_CHANNEL_ID, "Hello Discord!")

    def test_posts_multiple_chunks_for_long_content(self, httpx_mock):
        url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
        httpx_mock.add_response(url=url, json={"id": "1"}, status_code=200)
        httpx_mock.add_response(url=url, json={"id": "2"}, status_code=200)
        content = ("x" * 1000 + "\n") * 2
        with httpx.Client() as client:
            main.post_to_channel(client, DISCORD_CHANNEL_ID, content)
