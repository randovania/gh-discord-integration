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


def resolve_channel(owner: str, repo: str, labels: list[str], config: dict) -> str | None:
    """Return the channel name for an event or PR, or None if it should be ignored.

    Repos outside the configured org are ignored unless [repo_channels] lists them as "owner/repo".
    Once a repo is routable: a label mapping beats the repo mapping, labels pointing at more than one
    channel (e.g. several games) go to 'default', and anything unmapped goes to 'default'.
    """
    label_channels: dict[str, str] = config.get("label_channels", {})
    repo_channels: dict[str, str] = config.get("repo_channels", {})
    org = config.get("github", {}).get("org", "")

    repo_channel = repo_channels.get(f"{owner}/{repo}")
    if owner == org:
        repo_channel = repo_channel or repo_channels.get(repo, "default")
    elif repo_channel is None:
        return None

    matched = {label_channels[label] for label in labels if label in label_channels}
    if len(matched) == 1:
        return matched.pop()
    if len(matched) > 1:
        return "default"
    return repo_channel
