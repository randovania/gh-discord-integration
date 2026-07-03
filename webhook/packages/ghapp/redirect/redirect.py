import json
import logging
import os
import traceback
import requests

def get_labels(issue):
    return [
        label["name"]
        for label in issue["labels"]
    ]


_channel_for_label = {
    "game: AM2R": "am2r-dev",
    "game: Cave Story": "cave-story-dev",
    "game: Metroid Dread": "dread-dev",
    "game: Factorio": "factorio-dev",
    "game: Metroid Fusion": "fusion-dev",
    "game: Metroid Planets": "planets-dev",
    "game: Metroid Prime 1": "prime-dev",
    "game: Metroid Prime 2 Echoes": "echoes-dev",
    "game: Metroid Prime 3 Corruption": "corruption-dev",
    "game: Metroid Prime Hunters": "mp-hunters-dev",
    "game: Metroid: Samus Returns": "samus-returns-dev",
    "game: Metroid Zero Mission": "zero-mission-dev",
    "game: Super Metroid": "super-metroid-dev",
}

_channel_for_repository = {
    "YAMS": "am2r-dev",
    
    "factorio-assets-mod": "factorio-dev",
    "factorio-randovania-mod": "factorio-dev",
    
    "py-dolphin-memory-engine": "library-dev",
    "retro-data-structures": "library-dev",
    "randomprime": "prime-dev",
    "py-randomprime": "prime-dev",
    "open-prime-rando": "echoes-dev",
    "open-prime-rando-practice-mod": "echoes-dev",

    "open-prime-hunters-rando": "mp-hunters-dev",
    
    "mercury-engine-data-structures": "library-dev",
    "msr-remote-connector": "samus-returns-dev",
    "open-samus-returns-rando": "samus-returns-dev",
    "open-dread-rando": "dread-dev",
    "open-dread-rando-exlaunch": "dread-dev",
    
    "Super-Duper-Metroid": "super-metroid-dev",
}

_ignored_repositories = {}

ignored_users = {
    "codecov[bot]",
    "dependabot[bot]",
    "pre-commit-ci[bot]",
    "renovate[bot]",
}

def _send_to_discord(channel: str, body: dict):
    channel_enviroify = channel.upper().replace("-", "_")
    webhook = os.environ[f"WEBHOOK_{channel_enviroify}"]
    url = f"{webhook}/github"

    print(">> WILL POST TO", url)
    logging.info("post to: %s", url)

    r = requests.post(
        url,
        json=body,
        headers={
            "x-github-event": body["http"]["headers"]["x-github-event"]
        }
    )
    r.raise_for_status()
    logging.info("response: %s", str(r))
    return f"Posted to webhook: {r.text}" 
    

def process(args: dict[str, str]) -> dict:
    event = args["http"]["headers"]["x-github-event"]
    print(f"Received new event: {event}")

    if "repository" not in args:
        print("unsupported request")
        return {"body": "unsupported request"}

    if "sender" in args:
        if args["sender"].get("login") in ignored_users:
            print("ignored user")
            return {"body": "ignored user"}

    repository = args["repository"]["name"]

    issue = args.get("issue") or args.get("pull_request")
    if issue:
        if "user" in issue:
            if issue["user"].get("login") in ignored_users:
                print("ignored user")
                return {"body": "ignored user"}

        labels = get_labels(issue)
    else:
        labels = []

    channels = [_channel_for_label.get(label) for label in labels]
    channels = [c for c in channels if c is not None]

    if len(channels) == 1:
        channel_name = channels[0]
    elif repository in _channel_for_repository:
        channel_name = _channel_for_repository[repository]
    elif repository in _ignored_repositories:
        return {"body": "ignored repository"}
    else:
        channel_name = "randovania-dev"
    
    print(f"Event for repository {repository} and channel {channel_name}")
    # return {"body": f"Event for repository {repository} and channel {channel_name}"}

    result = _send_to_discord(channel_name, args)
    # print(result)
    return {"body": result}


def main(args: dict[str, str]):
    try:
        return process(args)
    except Exception as e:
        return {
            "body": traceback.format_exception(e)
        }
