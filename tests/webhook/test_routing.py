import routing

CONFIG = {
    "github": {"org": "randovania"},
    "label_channels": {"bug": "bugs-channel", "crash": "bugs-channel", "game: X": "x-channel"},
    "repo_channels": {
        "special-repo": "special-channel",
        "randovania/full-name-repo": "full-name-channel",
        "friend/their-repo": "friend-channel",
    },
}


def test_bundled_config_loads():
    config = routing.load_config()
    assert config["github"]["org"]
    assert config["discord"]["channels"]["default"]


def test_label_beats_repo():
    assert routing.resolve_channel("randovania", "special-repo", ["bug"], CONFIG) == "bugs-channel"


def test_labels_for_different_channels_go_to_default():
    assert routing.resolve_channel("randovania", "special-repo", ["bug", "game: X"], CONFIG) == "default"


def test_labels_for_same_channel_keep_it():
    assert routing.resolve_channel("randovania", "special-repo", ["bug", "crash"], CONFIG) == "bugs-channel"


def test_repo_match():
    assert routing.resolve_channel("randovania", "special-repo", [], CONFIG) == "special-channel"


def test_org_repo_can_use_full_name():
    assert routing.resolve_channel("randovania", "full-name-repo", [], CONFIG) == "full-name-channel"


def test_default_when_nothing_matches():
    assert routing.resolve_channel("randovania", "unknown-repo", [], CONFIG) == "default"


def test_other_org_is_ignored():
    assert routing.resolve_channel("someone", "unknown-repo", ["bug"], CONFIG) is None


def test_other_org_with_same_repo_name_is_ignored():
    # A bare-name mapping only applies to the org's own repos
    assert routing.resolve_channel("someone", "special-repo", [], CONFIG) is None


def test_other_org_explicitly_routed():
    assert routing.resolve_channel("friend", "their-repo", [], CONFIG) == "friend-channel"


def test_other_org_explicitly_routed_still_honours_labels():
    assert routing.resolve_channel("friend", "their-repo", ["bug"], CONFIG) == "bugs-channel"
