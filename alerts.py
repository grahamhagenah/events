#!/usr/bin/env python3
"""Open a GitHub issue for each source that has been failing for a while, and close it once it loads again.

Runs on GitHub after each build (see .github/workflows/deploy.yml), reading the failures the build recorded
in the JSON it publishes: python3 alerts.py dist/listings.json. GitHub emails the repo's owner about new
issues, so a source that breaks for good doesn't go unnoticed. A short hiccup doesn't count: a source must
fail every build for ALERT_AFTER first.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALERT_AFTER = timedelta(hours=6)
TITLE = "Source failing: "


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def describe(name, failure, now):
    hours = round((now - datetime.fromisoformat(failure["since"])).total_seconds() / 3600)
    return (
        f"{name} has failed every build for about {hours} hours, since {failure['since'][:16].replace('T', ' ')} UTC.\n\n"
        f"Last error: `{failure['error']}`\n\n"
        "Until it loads again, the page shows its last good listings for up to two days, then says it couldn't "
        "load it. If the site changed, its reader in build.py needs updating. This issue closes itself once "
        "the source loads again."
    )


def main():
    path = Path(sys.argv[1])
    if not path.exists():
        sys.exit(f"{path} wasn't written; the build failed, and GitHub reports that itself.")
    failing = json.loads(path.read_text()).get("failing", {})
    now = datetime.now(timezone.utc)

    listed = json.loads(gh("issue", "list", "--state", "open", "--search", f'"{TITLE}" in:title',
                           "--json", "number,title", "--limit", "100"))
    open_issues = {issue["title"][len(TITLE):]: issue["number"] for issue in listed if issue["title"].startswith(TITLE)}

    for name, failure in failing.items():
        if name not in open_issues and now - datetime.fromisoformat(failure["since"]) >= ALERT_AFTER:
            gh("issue", "create", "--title", TITLE + name, "--body", describe(name, failure, now))
            print(f"Opened an issue: {name} has been failing since {failure['since']}")
    for name, number in open_issues.items():
        if name not in failing:
            gh("issue", "close", str(number), "--comment", f"{name} is loading again.")
            print(f"Closed the issue for {name}: it's loading again")


if __name__ == "__main__":
    main()
