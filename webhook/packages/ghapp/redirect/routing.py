"""Config loading and channel routing shared by the DO function and the PR health report.

Lives next to config.toml inside the function directory, since DO only deploys that directory.
Standard library only: the DO function runs on Python 3.11, the report on 3.14.
"""

import tomllib
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


def load_config() -> dict:
    for candidate in (CONFIG_PATH, Path.cwd() / "config.toml"):
        if candidate.exists():
            return tomllib.loads(candidate.read_text(encoding="utf-8"))
    cwd_files = [p.name for p in Path.cwd().iterdir()]
    raise FileNotFoundError(f"config.toml not found at {CONFIG_PATH} or in cwd; cwd contains {cwd_files}")


def resolve_channels(owner: str, repo: str, labels: list[str], config: dict) -> list[str]:
    """Return the channel names for an event or PR; an empty list means it should be ignored.

    Repos outside the configured org are ignored unless [repo_channels] lists them as "owner/repo".
    A [repo_channels] entry is either a channel name, or a table with `channels` (a list) and optionally
    its own `label_channels`, which add to the global [label_channels] for that repo only.

    Once a repo is routable, labels pointing at exactly one channel win. Otherwise (no matching labels, or
    labels for several channels, e.g. several games) it goes to the repo's channels. For a plain channel
    name entry, labels for several channels go to 'default' instead.
    """
    repo_channels: dict = config.get("repo_channels", {})
    org = config.get("github", {}).get("org", "")

    entry = repo_channels.get(f"{owner}/{repo}")
    if entry is None and owner == org:
        entry = repo_channels.get(repo, "default")
    if entry is None:
        return []

    if isinstance(entry, str):
        fallback = [entry]
        on_conflict = ["default"]
        label_channels = config.get("label_channels", {})
    else:
        fallback = on_conflict = list(entry["channels"])
        label_channels = {**config.get("label_channels", {}), **entry.get("label_channels", {})}

    matched = {label_channels[label] for label in labels if label in label_channels}
    if len(matched) == 1:
        return [matched.pop()]
    if len(matched) > 1:
        return on_conflict
    return fallback
