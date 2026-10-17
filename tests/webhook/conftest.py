import copy
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import grouping
import pytest


class FakeDiscord:
    """In-memory stand-in for grouping.DiscordClient."""

    def __init__(self):
        self.channels: dict[str, list[dict]] = defaultdict(list)
        self.deleted: list[str] = []
        self._next_id = 1000
        self.fail_reads = False
        # One-shot hooks to simulate a parallel invocation acting at an awkward moment
        self.before_post = None
        self.after_patch = None

    def _find(self, channel_id: str, message_id: str) -> dict:
        return next(m for m in self.channels[channel_id] if m["id"] == message_id)

    def get_messages(self, channel_id: str) -> list[dict]:
        if self.fail_reads:
            raise RuntimeError("403 Missing Access")
        # Discord returns newest first
        return copy.deepcopy(list(reversed(self.channels[channel_id])))

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return copy.deepcopy(self._find(channel_id, message_id))

    def post(self, channel_id: str, embed: dict) -> dict:
        if hook := self.before_post:
            self.before_post = None
            hook(self, channel_id)
        self._next_id += 1
        message = {
            "id": str(self._next_id),
            "timestamp": grouping._now().isoformat(),
            "edited_timestamp": None,
            "embeds": [copy.deepcopy(embed)],
        }
        self.channels[channel_id].append(message)
        return copy.deepcopy(message)

    def patch(self, channel_id: str, message_id: str, embed: dict) -> dict:
        message = self._find(channel_id, message_id)
        previous = message["embeds"][0]
        message["embeds"] = [copy.deepcopy(embed)]
        message["edited_timestamp"] = grouping._now().isoformat()
        if hook := self.after_patch:
            self.after_patch = None
            hook(message, previous)
        return copy.deepcopy(message)

    def delete(self, channel_id: str, message_id: str) -> None:
        self.channels[channel_id] = [m for m in self.channels[channel_id] if m["id"] != message_id]
        self.deleted.append(message_id)

    def embeds(self, channel_id: str) -> list[dict]:
        return [m["embeds"][0] for m in self.channels[channel_id]]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 17, 7, 25, tzinfo=UTC)

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(grouping, "_sleep", lambda seconds: None)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(grouping, "_now", lambda: c.now)
    return c


@pytest.fixture
def discord(clock, monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr(grouping, "DiscordClient", lambda: fake)
    return fake
