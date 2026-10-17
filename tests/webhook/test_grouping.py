from datetime import timedelta

import grouping
import pytest

CHANNEL = "123"
WINDOW = timedelta(minutes=30)
AUTHOR = {"name": "alice", "url": "https://github.com/alice", "icon_url": ""}


def pr_event(action: str, number: int = 351, **extra) -> tuple[str, dict]:
    pr = {
        "number": number,
        "title": "MSAPI Parsing",
        "html_url": f"https://github.com/randovania/mercury/pull/{number}",
        "body": "Adds parsing.",
        "requested_reviewers": [],
        "requested_teams": [],
        "merged": False,
    }
    pr.update(extra.pop("pr", {}))
    return "pull_request", {
        "action": action,
        "pull_request": pr,
        "repository": {"name": "mercury"},
        "sender": {"login": "alice"},
        **extra,
    }


def review_requested(*names: str, number: int = 351) -> tuple[str, dict]:
    # The payload carries the full list requested so far, plus the one this event is about
    return pr_event(
        "review_requested",
        number,
        pr={"requested_reviewers": [{"login": n} for n in names]},
        requested_reviewer={"login": names[-1]},
    )


def approved(reviewer: str = "bob", body: str = "") -> tuple[str, dict]:
    _, args = pr_event("submitted")
    return "pull_request_review", {**args, "review": {"state": "approved", "user": {"login": reviewer}, "body": body}}


def send(discord, event_args: tuple[str, dict]) -> str:
    update = grouping.build_update(*event_args, owner="randovania", author=AUTHOR)
    assert update is not None
    return grouping.deliver(discord, CHANNEL, update, WINDOW)


def fields(embed: dict) -> dict:
    return {f["name"]: f["value"] for f in embed.get("fields", [])}


# ---------------------------------------------------------------------------
# Activity group
# ---------------------------------------------------------------------------


def test_opened_then_review_requests_become_one_message(discord):
    send(discord, pr_event("opened"))
    send(discord, review_requested("a"))
    send(discord, review_requested("a", "b"))
    send(discord, review_requested("a", "b", "c"))

    [embed] = discord.embeds(CHANNEL)
    assert "Pull request opened" in embed["title"]
    assert embed["description"] == "Adds parsing."
    assert fields(embed)["Reviewers"] == "a, b, c"
    assert embed["footer"]["text"] == "randovania/mercury#351"


def test_review_requests_without_opened_message_merge(discord):
    send(discord, review_requested("a"))
    send(discord, review_requested("a", "b"))

    [embed] = discord.embeds(CHANNEL)
    assert "review requested" in embed["title"]
    assert fields(embed)["Reviewers"] == "a, b"


def test_review_requested_restores_reviewer_lost_to_a_race(discord):
    send(discord, review_requested("a"))
    # Event for "b" was lost; the payload for "c" still lists everyone requested so far
    send(discord, review_requested("a", "b", "c"))
    assert fields(discord.embeds(CHANNEL)[0])["Reviewers"] == "a, b, c"


def test_team_review_request(discord):
    event, args = pr_event("review_requested", requested_team={"name": "maintainers"})
    send(discord, (event, args))
    assert fields(discord.embeds(CHANNEL)[0])["Reviewers"] == "maintainers"


def test_pushes_after_opening_are_counted(discord, clock):
    send(discord, pr_event("opened"))
    clock.advance(minutes=5)
    send(discord, pr_event("synchronize"))
    clock.advance(minutes=20)
    send(discord, pr_event("synchronize"))

    [embed] = discord.embeds(CHANNEL)
    assert "Pull request opened" in embed["title"]
    assert fields(embed)["Pushes"] == "+2"


def test_each_push_extends_the_window(discord, clock):
    send(discord, pr_event("opened"))
    for _ in range(3):
        clock.advance(minutes=25)
        send(discord, pr_event("synchronize"))
    assert len(discord.embeds(CHANNEL)) == 1
    assert fields(discord.embeds(CHANNEL)[0])["Pushes"] == "+3"


def test_push_after_quiet_window_posts_and_later_pushes_count_on_it(discord, clock):
    send(discord, pr_event("opened"))
    clock.advance(minutes=31)
    send(discord, pr_event("synchronize"))
    clock.advance(minutes=1)
    send(discord, pr_event("synchronize"))

    opened, pushed = discord.embeds(CHANNEL)
    assert "Pushes" not in fields(opened)
    assert "new commits" in pushed["title"]
    assert fields(pushed)["Pushes"] == "+1"


def test_other_pr_gets_its_own_message(discord):
    send(discord, pr_event("opened", number=1))
    send(discord, pr_event("opened", number=2))
    assert len(discord.embeds(CHANNEL)) == 2


# ---------------------------------------------------------------------------
# Merge group
# ---------------------------------------------------------------------------


def test_full_merge_flow_is_one_message(discord, clock):
    send(discord, approved("bob", body="LGTM\nthanks"))
    send(discord, pr_event("auto_merge_enabled"))
    clock.advance(minutes=1)
    send(discord, pr_event("enqueued"))
    clock.advance(minutes=20)
    send(discord, pr_event("closed", pr={"merged": True, "merged_by": {"login": "github-merge-queue"}}))
    send(discord, pr_event("dequeued", pr={"merged": True, "merged_by": {"login": "github-merge-queue"}}))

    [embed] = discord.embeds(CHANNEL)
    assert "Pull request merged" in embed["title"]
    assert embed["color"] == grouping._COLOR_PURPLE
    assert embed["description"].split("\n") == [
        "✅ Approved by bob: LGTM thanks",
        "🤖 Auto-merge enabled by alice",
        "⏳ Added to merge queue",
        "🟣 Merged by github-merge-queue",
    ]
    assert embed["footer"]["text"] == "randovania/mercury#351 · merge"


def test_dequeued_before_merged_still_combines(discord):
    merged = {"merged": True, "merged_by": {"login": "x"}}
    send(discord, pr_event("dequeued", pr=merged))
    result = send(discord, pr_event("closed", pr=merged))

    [embed] = discord.embeds(CHANNEL)
    assert "Pull request merged" in embed["title"]
    assert embed["description"] == "🟣 Merged by x"
    assert "already up to date" in result


def test_dequeued_for_ci_failure_then_requeued_and_merged(discord):
    send(discord, pr_event("enqueued"))
    send(discord, pr_event("dequeued", reason="CI_FAILURE"))
    assert "removed from merge queue" in discord.embeds(CHANNEL)[0]["title"]
    send(discord, pr_event("enqueued"))
    send(discord, pr_event("closed", pr={"merged": True, "merged_by": {"login": "x"}}))

    [embed] = discord.embeds(CHANNEL)
    assert "Pull request merged" in embed["title"]
    assert "↩️ Removed from merge queue (ci failure)" in embed["description"]


def test_auto_merge_arriving_before_approval_keeps_furthest_title(discord):
    send(discord, pr_event("auto_merge_enabled"))
    send(discord, approved())
    assert "auto-merge enabled" in discord.embeds(CHANNEL)[0]["title"]


def test_merge_flow_and_activity_are_separate_messages(discord):
    send(discord, pr_event("opened"))
    send(discord, approved())
    assert len(discord.embeds(CHANNEL)) == 2


def test_merge_after_window_posts_new_message(discord, clock):
    send(discord, approved())
    clock.advance(minutes=45)
    send(discord, pr_event("closed", pr={"merged": True}))
    assert len(discord.embeds(CHANNEL)) == 2


@pytest.mark.parametrize(
    ("event", "args"),
    [
        pr_event("closed"),
        pr_event("edited"),
        ("pull_request_review", {**pr_event("submitted")[1], "review": {"state": "changes_requested"}}),
        ("issues", {"action": "opened", "issue": {"number": 1}, "repository": {"name": "x"}}),
    ],
)
def test_ungrouped_events(event, args):
    assert grouping.build_update(event, args, owner="randovania", author=AUTHOR) is None


# ---------------------------------------------------------------------------
# Races and failures
# ---------------------------------------------------------------------------


def test_overwritten_edit_is_redone(discord):
    send(discord, pr_event("opened"))

    # A parallel invocation working from the old copy overwrites our edit right after we make it
    def stale_write(message, previous):
        message["embeds"] = [previous]

    discord.after_patch = stale_write
    send(discord, review_requested("a"))

    assert fields(discord.embeds(CHANNEL)[0])["Reviewers"] == "a"


def test_simultaneous_first_posts_keep_the_oldest(discord):
    # Another invocation posts for the same PR between our read and our post
    def racer_posts(fake, channel_id):
        update = grouping.build_update(*review_requested("a"), owner="randovania", author=AUTHOR)
        fake.post(channel_id, update.apply(None))

    discord.before_post = racer_posts
    result = send(discord, review_requested("a", "b"))

    [embed] = discord.embeds(CHANNEL)
    assert fields(embed)["Reviewers"] == "a, b"
    assert discord.deleted
    assert "removed duplicate" in result


def test_unreadable_channel_falls_back_to_posting(discord):
    discord.fail_reads = True
    result = send(discord, pr_event("opened"))
    assert len(discord.embeds(CHANNEL)) == 1
    assert "Could not read channel" in result


def test_written_embed_has_only_writable_keys(discord):
    send(discord, pr_event("opened"))
    # Discord adds read-only keys to embeds it returns
    message = discord.channels[CHANNEL][0]
    message["embeds"][0] |= {"type": "rich"}
    message["embeds"][0]["author"]["proxy_icon_url"] = "https://proxy"
    send(discord, review_requested("a"))

    embed = discord.embeds(CHANNEL)[0]
    assert "type" not in embed
    assert "proxy_icon_url" not in embed["author"]


def test_edits_the_oldest_of_two_matching_messages(discord):
    send(discord, review_requested("a"))
    # A duplicate from a race that hasn't been cleaned up yet
    update = grouping.build_update(*review_requested("a"), owner="randovania", author=AUTHOR)
    discord.post(CHANNEL, update.apply(None))

    send(discord, review_requested("a", "b"))
    first, second = discord.embeds(CHANNEL)
    assert fields(first)["Reviewers"] == "a, b"
    assert fields(second)["Reviewers"] == "a"


def test_target_deleted_mid_edit_looks_again(discord):
    send(discord, pr_event("opened"))

    def deleted_by_someone_else(fake, channel_id, message_id):
        fake.delete(channel_id, message_id)

    discord.before_patch = deleted_by_someone_else
    send(discord, review_requested("a"))

    [embed] = discord.embeds(CHANNEL)
    assert "review requested" in embed["title"]
    assert fields(embed)["Reviewers"] == "a"


def test_keeps_writing_when_always_overwritten(discord):
    send(discord, pr_event("opened"))

    def always_stale(message, previous):
        message["embeds"] = [previous]
        return True

    discord.after_patch = always_stale
    result = send(discord, review_requested("a"))

    # Two verified attempts, then one final write
    assert discord.patches == 3
    assert "after retries" in result
