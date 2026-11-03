#!/usr/bin/env python3
"""Rebuild the "// activity" block of README.md from your public GitHub events.

Why a custom script?  GitHub trimmed the Events API payloads (announced Aug 2025):
pushes and pull requests now only come with ids (no commit list, no PR title, no
merged flag), and ready-made actions read exactly those fields, so they show
almost nothing.  This script lists your events, then asks the normal REST API
for the missing details.

Standard library only.  Runs in GitHub Actions, or locally:
    GH_USERNAME=jeetjawale GITHUB_TOKEN=<token> python3 update_activity.py

Settings (environment variables, all optional):
    GH_USERNAME   whose activity to show             default: the repo owner
    GITHUB_TOKEN  lifts the API rate limit           the workflow passes it in
    TARGET_FILE   file that holds the two markers    default: README.md
    MAX_LINES     how many lines to show             default: 10
    TIMEZONE      timezone for the date labels       default: Asia/Kolkata
    SHOW_STARS    "true" to also list starred repos  default: false
    IGNORE_REPOS  comma-separated repos to skip      default: <user>/<user>
                  (your profile repo, otherwise every README tweak shows up)
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

USER = os.environ.get("GH_USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER") or ""
TOKEN = os.environ.get("GITHUB_TOKEN", "")
TARGET = os.environ.get("TARGET_FILE") or "README.md"
MAX_LINES = int(os.environ.get("MAX_LINES") or 10)
SHOW_STARS = (os.environ.get("SHOW_STARS") or "").lower() == "true"
IGNORE = {
    r.strip().lower()
    for r in (os.environ.get("IGNORE_REPOS") or f"{USER}/{USER}").split(",")
    if r.strip()
}
try:
    TZ = ZoneInfo(os.environ.get("TIMEZONE") or "Asia/Kolkata")
except (KeyError, ValueError):  # unknown timezone name
    TZ = timezone.utc

START = "<!--START_SECTION:activity-->"
END = "<!--END_SECTION:activity-->"
ZERO_SHA = "0" * 40
REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")

PR_VERBS = {
    "opened": "💪 Opened PR",
    "merged": "🎉 Merged PR",
    "closed": "❌ Closed PR",
    "reopened": "🔄 Reopened PR",
}
REVIEW_VERBS = {"approved": "✅ Approved PR", "changes_requested": "🔧 Requested changes on PR"}
ISSUE_VERBS = {"opened": "❗ Opened issue", "closed": "🔒 Closed issue", "reopened": "🔄 Reopened issue"}


# --------------------------------------------------------------------- GitHub API
def api(path):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "readme-activity-script",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request("https://api.github.com" + path, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


_cache = {}


def lookup(path):
    """Best-effort REST lookup, cached.  Returns None when GitHub says no."""
    if path not in _cache:
        try:
            _cache[path] = api(path)
        except (OSError, ValueError):  # HTTP errors, timeouts, bad JSON
            _cache[path] = None
    return _cache[path]


def fetch_events():
    events = []
    for page in (1, 2, 3):  # the API serves at most 300 events
        batch = api(f"/users/{USER}/events/public?per_page=100&page={page}")
        events += batch
        if len(batch) < 100:
            break
    return events


# ------------------------------------------------------------------ text helpers
def md(text, limit=60):
    """Untrusted text (titles, commit messages) -> one line of safe Markdown.

    Titles can come from other people's repos, so every character that could
    turn into a link, image, HTML or our own section markers gets escaped.
    """
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return re.sub(r"([\\`*_\[\]<>()!|~&$])", r"\\\1", text)


def code(text, limit=28):
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return "`" + text.replace("`", "'") + "`"


def parse_time(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(TZ)


def when_label(moment):
    return f"{moment.day} {moment:%b}"


# ------------------------------------------------------------- events -> items
def find_merge_commits(events):
    """SHAs of the merge/squash commits made by PRs you merged.

    Merging a PR also shows up as a push to the base branch; knowing these SHAs
    lets us hide that push because the "Merged PR" line already covers it.
    """
    shas, checked = set(), set()
    for e in events:
        p = e.get("payload") or {}
        if e.get("type") != "PullRequestEvent" or p.get("action") not in ("closed", "merged"):
            continue
        repo = (e.get("repo") or {}).get("name", "")
        number = p.get("number") or (p.get("pull_request") or {}).get("number")
        if not REPO_RE.fullmatch(repo) or not isinstance(number, int):
            continue
        if (repo, number) in checked or len(checked) >= 15:
            continue
        checked.add((repo, number))
        pr = lookup(f"/repos/{repo}/pulls/{number}") or {}
        if pr.get("merged") and pr.get("merge_commit_sha"):
            shas.add(pr["merge_commit_sha"])
    return shas


def build_items(events):
    """Turn raw events into display items, newest first."""
    merge_shas = find_merge_commits(events)
    items, seen, pushes = [], set(), {}

    def add(key, **item):
        if key not in seen:  # one line per PR / issue / release, not one per event
            seen.add(key)
            items.append(item)

    for e in events:
        repo = (e.get("repo") or {}).get("name", "")
        if not REPO_RE.fullmatch(repo) or repo.lower() in IGNORE:
            continue
        kind, p = e.get("type"), e.get("payload") or {}
        base = {"repo": repo, "when": parse_time(e["created_at"])}

        if kind == "PushEvent":
            ref, before, head = p.get("ref", ""), p.get("before", ""), p.get("head", "")
            if not ref.startswith("refs/heads/") or head == ZERO_SHA:
                continue  # tag pushes and branch deletions
            if not (SHA_RE.fullmatch(before) and SHA_RE.fullmatch(head)) or head in merge_shas:
                continue
            branch = ref.removeprefix("refs/heads/")
            key = (repo, branch, base["when"].date())
            if key in pushes:  # an earlier push to the same branch on the same day
                pushes[key]["before"] = before
            else:
                pushes[key] = {**base, "kind": "push", "branch": branch, "before": before, "head": head}
                items.append(pushes[key])

        elif kind == "PullRequestEvent":
            action = p.get("action")
            number = p.get("number") or (p.get("pull_request") or {}).get("number")
            if action in PR_VERBS and isinstance(number, int):
                step = "end" if action in ("closed", "merged") else action
                add(("pr", repo, number, step), kind="pr", number=number, action=action, **base)

        elif kind in ("PullRequestReviewEvent", "PullRequestReviewCommentEvent"):
            number = (p.get("pull_request") or {}).get("number")
            if isinstance(number, int) and p.get("action", "created") == "created":
                state = str((p.get("review") or {}).get("state", "")).lower()
                add(("review", repo, number), kind="review", number=number, state=state, **base)

        elif kind == "IssuesEvent":
            issue = p.get("issue") or {}
            number, action = issue.get("number"), p.get("action")
            if action in ISSUE_VERBS and isinstance(number, int):
                add(("issue", repo, number, action), kind="issue", number=number,
                    action=action, title=issue.get("title"), **base)

        elif kind == "IssueCommentEvent":
            issue, comment = p.get("issue") or {}, p.get("comment") or {}
            number = issue.get("number")
            if isinstance(number, int) and p.get("action", "created") == "created":
                add(("comment", repo, number), kind="comment", number=number,
                    title=issue.get("title"), is_pr="pull_request" in issue,
                    comment_id=comment.get("id"), **base)

        elif kind == "ReleaseEvent":
            release = p.get("release") or {}
            tag = release.get("tag_name")
            if p.get("action") == "published" and tag:
                add(("release", repo, tag), kind="release", tag=tag,
                    name=release.get("name") or tag, **base)

        elif kind == "CreateEvent":
            ref_type, ref = p.get("ref_type"), p.get("ref")
            if ref_type == "repository":
                add(("created", repo), kind="repo_created", **base)
            elif ref_type == "tag" and ref:
                add(("tag", repo, ref), kind="tag", tag=ref, **base)

        elif kind == "ForkEvent":
            fork = (p.get("forkee") or {}).get("full_name", "")
            add(("fork", repo), kind="fork", fork=fork if REPO_RE.fullmatch(fork) else "", **base)

        elif kind == "PublicEvent":
            add(("public", repo), kind="public", **base)

        elif kind == "WatchEvent" and SHOW_STARS:
            add(("star", repo), kind="star", **base)

    released = {(i["repo"], i["tag"]) for i in items if i["kind"] == "release"}
    return [i for i in items if not (i["kind"] == "tag" and (i["repo"], i["tag"]) in released)]


# ------------------------------------------------------------- items -> Markdown
def render_push(item, where):
    repo, before, head = item["repo"], item["before"], item["head"]
    count, message = None, ""
    if before != ZERO_SHA:  # a brand-new branch has nothing to compare against
        diff = lookup(f"/repos/{repo}/compare/{before}...{head}")
        if diff:
            count = diff.get("total_commits")
            commits = diff.get("commits") or []
            if commits:
                message = (commits[-1].get("commit") or {}).get("message", "")
    if not message:
        commit = lookup(f"/repos/{repo}/commits/{head}")
        message = ((commit or {}).get("commit") or {}).get("message", "")
    subject = message.split("\n", 1)[0]

    if isinstance(count, int) and count > 1:
        label = f"{count} commits"
        url = f"https://github.com/{repo}/compare/{before}...{head}"
    else:
        label = "1 commit" if count == 1 else "commits"
        url = f"https://github.com/{repo}/commit/{head}"
    line = f"⬆️ Pushed [{label}]({url}) to {code(item['branch'])} in {where}"
    return line + (f" — *{md(subject)}*" if subject else "")


def render(item):
    repo, kind = item["repo"], item["kind"]
    where = f"[{repo}](https://github.com/{repo})"

    if kind == "push":
        return render_push(item, where)

    if kind == "pr":
        number = item["number"]
        pr = lookup(f"/repos/{repo}/pulls/{number}") or {}
        merged = item["action"] == "merged" or (item["action"] == "closed" and pr.get("merged"))
        verb = PR_VERBS["merged" if merged else item["action"]]
        label = f"#{number} {md(pr.get('title'))}".strip()
        return f"{verb} [{label}](https://github.com/{repo}/pull/{number}) in {where}"

    if kind == "review":
        number = item["number"]
        pr = lookup(f"/repos/{repo}/pulls/{number}") or {}
        verb = REVIEW_VERBS.get(item["state"], "👀 Reviewed PR")
        label = f"#{number} {md(pr.get('title'))}".strip()
        return f"{verb} [{label}](https://github.com/{repo}/pull/{number}) in {where}"

    if kind == "issue":
        number = item["number"]
        title = item.get("title") or (lookup(f"/repos/{repo}/issues/{number}") or {}).get("title")
        label = f"#{number} {md(title)}".strip()
        verb = ISSUE_VERBS[item["action"]]
        return f"{verb} [{label}](https://github.com/{repo}/issues/{number}) in {where}"

    if kind == "comment":
        number, title, is_pr = item["number"], item.get("title"), item.get("is_pr")
        if not title:
            issue = lookup(f"/repos/{repo}/issues/{number}") or {}
            title, is_pr = issue.get("title"), "pull_request" in issue
        comment_id = item.get("comment_id")
        anchor = f"#issuecomment-{comment_id}" if isinstance(comment_id, int) else ""
        path = "pull" if is_pr else "issues"
        label = f"#{number} {md(title)}".strip()
        return f"🗣 Commented on [{label}](https://github.com/{repo}/{path}/{number}{anchor}) in {where}"

    if kind == "release":
        tag = urllib.parse.quote(item["tag"], safe="/")
        return f"🚀 Published release [{md(item['name'])}](https://github.com/{repo}/releases/tag/{tag}) in {where}"

    if kind == "tag":
        tag = urllib.parse.quote(item["tag"], safe="/")
        return f"🏷️ Tagged [{md(item['tag'], 30)}](https://github.com/{repo}/tree/{tag}) in {where}"

    if kind == "repo_created":
        return f"📦 Created repository {where}"

    if kind == "fork":
        fork = item.get("fork")
        return f"🍴 Forked {where}" + (f" to [{fork}](https://github.com/{fork})" if fork else "")

    if kind == "public":
        return f"🌍 Made {where} public"

    if kind == "star":
        return f"⭐ Starred {where}"

    return None


# ----------------------------------------------------------------------- output
def write_section(lines):
    with open(TARGET, encoding="utf-8") as handle:
        text = handle.read()
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if not pattern.search(text):
        sys.exit(f"Could not find {START} ... {END} in {TARGET}.")
    body = "\n".join(f"{n}. {line}" for n, line in enumerate(lines, 1))
    # a function (not a string) as replacement, so backslashes in `body` stay literal
    updated = pattern.sub(lambda _: f"{START}\n{body}\n{END}", text, count=1)
    if updated != text:
        with open(TARGET, "w", encoding="utf-8") as handle:
            handle.write(updated)


def main():
    if not USER:
        sys.exit("No username: set GH_USERNAME (inside GitHub Actions it defaults to the repo owner).")
    try:
        events = fetch_events()
    except urllib.error.HTTPError as err:
        sys.exit(f"GitHub answered HTTP {err.code} while listing events for {USER} (rate limit? token?).")

    lines = []
    for item in build_items(events):
        text = render(item)
        if text:
            lines.append(f"{text} · <sub>{when_label(item['when'])}</sub>")
        if len(lines) == MAX_LINES:
            break

    if not lines:
        print(f"No public activity found for {USER}; leaving {TARGET} as it is.")
        return
    write_section(lines)
    print("\n".join(f"{n}. {line}" for n, line in enumerate(lines, 1)))


if __name__ == "__main__":
    main()
