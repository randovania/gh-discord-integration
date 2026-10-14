import pytest
import routing

CONFIG = {
    "github": {"org": "randovania"},
    "label_channels": {"bug": "bugs-channel", "crash": "bugs-channel", "game: X": "x-channel"},
    "repo_channels": {
        "special-repo": "special-channel",
        "randovania/full-name-repo": "full-name-channel",
        "friend/their-repo": "friend-channel",
        "friend/multi": {
            "channels": ["a-channel", "b-channel"],
            "label_channels": {"game: a": "a-channel", "game: b": "b-channel"},
        },
    },
}


def test_bundled_config_loads():
    config = routing.load_config()
    assert config["github"]["org"]
    assert config["discord"]["channels"]["default"]


def test_label_beats_repo():
    assert routing.resolve_channels("randovania", "special-repo", ["bug"], CONFIG) == ["bugs-channel"]


def test_labels_for_different_channels_go_to_default():
    assert routing.resolve_channels("randovania", "special-repo", ["bug", "game: X"], CONFIG) == ["default"]


def test_labels_for_same_channel_keep_it():
    assert routing.resolve_channels("randovania", "special-repo", ["bug", "crash"], CONFIG) == ["bugs-channel"]


def test_repo_match():
    assert routing.resolve_channels("randovania", "special-repo", [], CONFIG) == ["special-channel"]


def test_org_repo_can_use_full_name():
    assert routing.resolve_channels("randovania", "full-name-repo", [], CONFIG) == ["full-name-channel"]


def test_default_when_nothing_matches():
    assert routing.resolve_channels("randovania", "unknown-repo", [], CONFIG) == ["default"]


def test_other_org_is_ignored():
    assert routing.resolve_channels("someone", "unknown-repo", ["bug"], CONFIG) == []


def test_other_org_with_same_repo_name_is_ignored():
    # A bare-name mapping only applies to the org's own repos
    assert routing.resolve_channels("someone", "special-repo", [], CONFIG) == []


def test_other_org_explicitly_routed():
    assert routing.resolve_channels("friend", "their-repo", [], CONFIG) == ["friend-channel"]


def test_other_org_explicitly_routed_still_honours_labels():
    assert routing.resolve_channels("friend", "their-repo", ["bug"], CONFIG) == ["bugs-channel"]


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["game: a"], ["a-channel"]),
        (["game: b", "enhancement"], ["b-channel"]),
        ([], ["a-channel", "b-channel"]),
        (["game: a", "game: b"], ["a-channel", "b-channel"]),
        # Global labels still apply to a table entry
        (["bug"], ["bugs-channel"]),
    ],
)
def test_table_entry(labels, expected):
    assert routing.resolve_channels("friend", "multi", labels, CONFIG) == expected


def test_repo_specific_labels_dont_leak_to_other_repos():
    assert routing.resolve_channels("friend", "their-repo", ["game: a"], CONFIG) == ["friend-channel"]


@pytest.mark.parametrize(
    ("repo", "labels", "expected"),
    [
        ("mars-fusion-asm", [], ["metroid-fusion-dev"]),
        ("mars-mzm-decomp", [], ["metroid-zero-mission-dev"]),
        ("mars-patcher-py", ["game: mf"], ["metroid-fusion-dev"]),
        ("mars-patcher-py", ["game: zm"], ["metroid-zero-mission-dev"]),
        ("mars-patcher-py", [], ["metroid-fusion-dev", "metroid-zero-mission-dev"]),
        ("mars-patcher-py", ["game: mf", "game: zm"], ["metroid-fusion-dev", "metroid-zero-mission-dev"]),
        ("some-other-repo", [], []),
    ],
)
def test_bundled_config_mars_routing(repo, labels, expected):
    config = routing.load_config()
    channels = routing.resolve_channels("MetroidAdvRandomizerSystem", repo, labels, config)
    assert channels == expected
    assert all(channel in config["discord"]["channels"] for channel in channels)


@pytest.mark.parametrize(
    ("owner", "repo", "expected"),
    [
        ("MetroidAdvRandomizerSystem", "mars-fusion-asm", True),
        ("MetroidAdvRandomizerSystem", "mars-mzm-decomp", True),
        ("MetroidAdvRandomizerSystem", "mars-patcher-py", True),
        ("MetroidAdvRandomizerSystem", "some-other-repo", False),
        ("randovania", "randovania", False),
        ("randovania", "YAMS", False),
    ],
)
def test_bundled_config_push_repos(owner, repo, expected):
    assert routing.wants_push(owner, repo, routing.load_config()) is expected


def test_push_repos_bare_name_only_matches_org():
    config = {"github": {"org": "randovania"}, "webhook": {"push_repos": ["YAMS"]}}
    assert routing.wants_push("randovania", "YAMS", config)
    assert not routing.wants_push("someone", "YAMS", config)


def test_push_disabled_without_webhook_section():
    assert not routing.wants_push("randovania", "YAMS", {"github": {"org": "randovania"}})
