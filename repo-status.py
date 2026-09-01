#!/usr/bin/env python3
"""Report on the GitHub projects you own, and reconcile them with your local clones.

Sections, in the order they print — the order the work gets done in, with the
clone-tree housekeeping at either end:

  reconcile        the clone tree under --root vs the repos you own on GitHub,
                   each clone expected at <root>/<repo name>
  uncommitted      dirty working trees, and commits no remote holds
  local-branches   local branches already merged into the default branch
  orphan-branches  remote branches with no open PR that have gone stale
  prs              open pull requests
  unreleased       commits on the default branch since the last release or tag
  issues           open issues, grouped by milestone
  behind           clones trailing origin, and the fetches that failed

Remote state comes from the GitHub API through `gh`, so it reflects the server
rather than whatever the clones happen to hold. Local state comes from the
clones themselves, each one fetched with --prune first.

Without --fix the run only reads (and fetches). With --fix it clones what is
missing and fast-forwards what is cleanly behind, then asks y/n before each
change that moves or removes something: relocating a clone onto its own path,
deleting a clone whose repo is gone, deleting a merged local branch, deleting an
orphaned remote branch.

Every run writes the findings to output/repo-status.html as well as the terminal
report; --open opens it.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta, timezone

import yaml

SECTIONS = ["reconcile", "uncommitted", "local-branches", "orphan-branches",
            "prs", "unreleased", "issues", "behind"]


IGNORE_FILE = "ignore.yml"
IGNORE_KEYS = {"archived", "forks", "repos"}

# Every reason a repo stays out of a run, and the flag that widens the run to
# take it back in.
HOLD_WIDENS = {IGNORE_FILE: "--no-ignore", "archived": "--include-archived",
               "fork": "--include-forks"}


class IgnoreError(RuntimeError):
    pass


class Ignore:
    def __init__(self, archived=False, forks=False, names=()):
        self.archived = archived
        self.forks = forks
        self.names = {n.lower() for n in names}

    def skips(self, name, archived=False, fork=False):
        return self.reason(name, archived, fork) is not None

    def reason(self, name, archived=False, fork=False):
        if (name or "").lower() in self.names:
            return IGNORE_FILE
        if self.archived and archived:
            return "archived"
        return "fork" if self.forks and fork else None

    def __bool__(self):
        return bool(self.archived or self.forks or self.names)

    def describe(self):
        parts = [state for state, on in (("archived", self.archived),
                                         ("forks", self.forks)) if on]
        if self.names:
            parts.append(", ".join(sorted(self.names)))
        return "; ".join(parts) or "nothing"


def load_ignore(enabled=True):
    """Read the ignore list, from the script's directory rather than the caller's."""
    if not enabled:
        return Ignore()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), IGNORE_FILE)
    if not os.path.exists(path):
        return Ignore()
    with open(path, encoding="utf-8") as handle:
        try:
            parsed = yaml.safe_load(handle) or {}
        except yaml.YAMLError as err:
            raise IgnoreError(f"{IGNORE_FILE}: {err}") from err
    if not isinstance(parsed, dict):
        raise IgnoreError(f"{IGNORE_FILE}: expected `key: value` entries")
    # An unrecognized key would widen a scan in silence, which is the one
    # failure mode an ignore list cannot have.
    unknown = set(parsed) - IGNORE_KEYS
    if unknown:
        raise IgnoreError(f"{IGNORE_FILE}: unknown key {', '.join(sorted(unknown))!r}, "
                          f"expected one of {', '.join(sorted(IGNORE_KEYS))}")
    states = {}
    for key in ("archived", "forks"):
        states[key] = parsed.get(key, False)
        if not isinstance(states[key], bool):
            raise IgnoreError(f"{IGNORE_FILE}: `{key}` takes true or false")
    repos = parsed.get("repos") or []
    if not isinstance(repos, list):
        raise IgnoreError(f"{IGNORE_FILE}: `repos` takes a list of `- name` entries")
    return Ignore(archived=states["archived"], forks=states["forks"], names=repos)


class GhError(RuntimeError):
    pass


class GitError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# process helpers
# --------------------------------------------------------------------------- #

def gh(args):
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def gh_api(path, jq=None):
    args = ["api", path]
    if jq:
        args += ["--jq", jq]
    return gh(args)


def gh_json(path):
    return json.loads(gh_api(path))


def gh_graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F", f"{key}={value}"]
    return json.loads(gh(args))


def git(path, *args, check=True):
    """Run git in `path`; returns stripped stdout, or None on failure when check=False."""
    proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        if check:
            raise GitError(proc.stderr.strip() or f"git {' '.join(args)} exited {proc.returncode}")
        return None
    return proc.stdout.strip()


def prune_empty(directory, root):
    """Drop the grouping directories a move just emptied, stopping at root."""
    directory = os.path.abspath(directory)
    root = os.path.abspath(root)
    while directory != root and directory.startswith(root + os.sep):
        try:
            os.rmdir(directory)
        except OSError:
            return
        print(f"      removed empty {directory}")
        directory = os.path.dirname(directory)


def first_line(text):
    """git leads its failure output with the cause and trails into boilerplate."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def current_login():
    return gh_api("user", ".login").strip()


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

REMOTE_URL = re.compile(
    r"^(?:git@[^:]+:|(?:https?|ssh|git)://[^/]+/)(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


def parse_remote(url):
    match = REMOTE_URL.match(url.strip())
    if not match:
        return None, None
    return match.group("owner"), match.group("name")


def find_clones(root, depth):
    """Every git work tree under `root`, without descending into one already found."""
    clones = []
    root = os.path.abspath(os.path.expanduser(root))

    def walk(directory, level):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                continue
            if os.path.isdir(os.path.join(entry.path, ".git")):
                url = git(entry.path, "remote", "get-url", "origin", check=False)
                owner, name = parse_remote(url) if url else (None, None)
                clones.append({
                    "path": entry.path,
                    "rel": os.path.relpath(entry.path, root),
                    "origin": url,
                    "owner": owner,
                    "name": name,
                })
            elif level < depth:
                walk(entry.path, level + 1)

    walk(root, 1)
    return clones


def list_repos(owner, limit):
    args = ["repo", "list", owner, "--limit", str(limit), "--json",
            "name,owner,defaultBranchRef,isPrivate,isFork,isArchived,pushedAt,url,sshUrl"]
    return json.loads(gh(args))


def hold_back(repo, ignore):
    """Why a repo sits outside this run's scope, or None when it is in scope.

    The whole account is listed so a held-back repo can be named and counted;
    holding it back here is what keeps it from costing an API call later."""
    return ignore.reason(repo["name"], repo["isArchived"], repo["isFork"])


def repo_record(full):
    info = gh_json(f"repos/{full}")
    return {
        "name": info["name"],
        "owner": {"login": info["owner"]["login"]},
        "defaultBranchRef": {"name": info["default_branch"]} if info["default_branch"] else None,
        "isPrivate": info["private"],
        "isFork": info["fork"],
        "isArchived": info["archived"],
        "pushedAt": info["pushed_at"],
        "url": info["html_url"],
        "sshUrl": info["ssh_url"],
    }


# --------------------------------------------------------------------------- #
# remote probes
# --------------------------------------------------------------------------- #

def latest_release(full_name):
    """Latest published release, or None when the repo has never released one."""
    try:
        rel = gh_json(f"repos/{full_name}/releases/latest")
    except GhError as err:
        if "Not Found" in str(err) or "404" in str(err):
            return None
        raise
    return {
        "kind": "release",
        "tag": rel["tag_name"],
        "name": rel.get("name") or rel["tag_name"],
        "published": rel.get("published_at"),
        "url": rel.get("html_url"),
        "prerelease": rel.get("prerelease", False),
    }


def latest_tag(full_name):
    """Newest tag by commit date — the marker for repos that tag but don't release."""
    tags = gh_json(f"repos/{full_name}/tags?per_page=100")
    if not tags:
        return None
    dated = []
    for tag in tags:
        commit = gh_json(f"repos/{full_name}/commits/{tag['commit']['sha']}")
        dated.append((commit["commit"]["committer"]["date"], tag["name"]))
    date, name = max(dated)
    return {
        "kind": "tag",
        "tag": name,
        "name": name,
        "published": date,
        "url": f"https://github.com/{full_name}/releases/tag/{name}",
        "prerelease": False,
    }


RELEASE_PREFIX = re.compile(r"^\s*(release|releasing|bump|version|chore)\b", re.IGNORECASE)


def is_release_commit(subject, tag):
    """True for the version-bump commit belonging to `tag` itself.

    Repos that tag before bumping (tag on the parent, `Release vX` as its child)
    leave that commit sitting on the unreleased side of the compare, where it
    reads as pending work it isn't. Matching demands the subject name this exact
    version, so an ordinary commit mentioning a release can't be swept up.
    """
    version = tag.lstrip("vV")
    if not version:
        return False
    # A dotless version ("1") is too weak a token to match on its own, so a tag
    # like v1 has to be named in full.
    forms = {tag} | ({version} if "." in version else set())
    named = any(re.search(rf"(?<![\w.]){re.escape(v)}(?![\w.])", subject) for v in forms)
    if not named:
        return False
    return bool(RELEASE_PREFIX.match(subject)) or subject.strip().rstrip(".") in {tag, version}


def unreleased(full_name, base_tag, branch):
    data = gh_json(f"repos/{full_name}/compare/{base_tag}...{branch}")
    commits = []
    for c in data.get("commits", []):
        subject = c["commit"]["message"].split("\n", 1)[0]
        commits.append({
            "sha": c["sha"][:7],
            "subject": subject,
            "author": (c.get("author") or {}).get("login")
            or c["commit"]["author"]["name"],
            "date": c["commit"]["author"]["date"],
            "release_commit": is_release_commit(subject, base_tag),
        })
    ahead = data.get("ahead_by", 0)
    ceremony = sum(1 for c in commits if c["release_commit"])
    # The compare API hands back commits oldest first, so the head of each list
    # is the change that has waited longest — the report's sort key.
    pending = [c for c in commits if not c["release_commit"]]
    return {
        "ahead": ahead - ceremony,
        "ahead_raw": ahead,
        "behind": data.get("behind_by", 0),
        "commits": commits,
        "oldest": pending[0]["date"] if pending else None,
        "oldest_raw": commits[0]["date"] if commits else None,
        "truncated": data.get("total_commits", 0) > len(commits),
    }


BRANCH_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100) {
      totalCount
      nodes { name target { ... on Commit { committedDate oid } } }
    }
  }
}
"""


def remote_branches(owner, name):
    data = gh_graphql(BRANCH_QUERY, owner=owner, name=name)
    refs = ((data.get("data") or {}).get("repository") or {}).get("refs") or {}
    branches = []
    for node in refs.get("nodes") or []:
        target = node.get("target") or {}
        branches.append({
            "name": node["name"],
            "date": target.get("committedDate"),
            "sha": (target.get("oid") or "")[:7],
        })
    return branches, refs.get("totalCount", len(branches)) > len(branches)


def open_pulls(full_name):
    pulls = gh_json(f"repos/{full_name}/pulls?state=open&per_page=100")
    return [{
        "number": p["number"],
        "title": p["title"],
        "author": (p.get("user") or {}).get("login"),
        "head": (p.get("head") or {}).get("ref"),
        "base": (p.get("base") or {}).get("ref"),
        "draft": p.get("draft", False),
        "created": p.get("created_at"),
        "updated": p.get("updated_at"),
        "url": p["html_url"],
    } for p in pulls]


def open_issues(full_name):
    issues = gh_json(f"repos/{full_name}/issues?state=open&per_page=100")
    out = []
    for i in issues:
        if "pull_request" in i:
            continue
        milestone = i.get("milestone") or {}
        out.append({
            "number": i["number"],
            "title": i["title"],
            "author": (i.get("user") or {}).get("login"),
            "assignees": [a["login"] for a in i.get("assignees") or []],
            "labels": [l["name"] for l in i.get("labels") or []],
            "milestone": milestone.get("title"),
            "milestone_due": milestone.get("due_on"),
            "created": i.get("created_at"),
            "updated": i.get("updated_at"),
            "url": i["html_url"],
        })
    return out


def probe_repo(repo, wanted, max_commits):
    full_name = f"{repo['owner']['login']}/{repo['name']}"
    branch_ref = repo.get("defaultBranchRef") or {}
    branch = branch_ref.get("name")
    result = {
        "repo": full_name,
        "name": repo["name"],
        "owner": repo["owner"]["login"],
        "url": repo["url"],
        "ssh_url": repo.get("sshUrl") or f"git@github.com:{full_name}.git",
        "private": repo["isPrivate"],
        "fork": repo["isFork"],
        "archived": repo["isArchived"],
        "branch": branch,
        "pushed_at": repo["pushedAt"],
        "release": None,
        "ahead": 0,
        "ahead_raw": 0,
        "behind": 0,
        "commits": [],
        "oldest": None,
        "oldest_raw": None,
        "truncated": False,
        "pulls": [],
        "issues": [],
        "branches": [],
        "branches_truncated": False,
        "errors": {},
    }

    # Each probe fails on its own. A repo with pull requests turned off 404s on
    # /pulls while its issues and tags answer fine, and one shared error field
    # would both abandon those and file the failure under whichever section read
    # it first.
    def attempt(area, call):
        try:
            return call()
        except GhError as err:
            result["errors"][area] = str(err)
            return None

    if "orphan-branches" in wanted:
        got = attempt("branches", lambda: remote_branches(result["owner"], result["name"]))
        if got:
            result["branches"], result["branches_truncated"] = got
    if wanted & {"prs", "orphan-branches"}:
        result["pulls"] = attempt("pulls", lambda: open_pulls(full_name)) or []
    if "issues" in wanted:
        result["issues"] = attempt("issues", lambda: open_issues(full_name)) or []
    # An empty repo has no baseline and no commits; it drops out of the
    # unreleased section with the rest of the never-released projects.
    if "unreleased" in wanted and branch:
        def probe_unreleased():
            marker = latest_release(full_name) or latest_tag(full_name)
            result["release"] = marker
            if marker:
                diff = unreleased(full_name, marker["tag"], branch)
                result.update(diff)
                result["commits"] = list(reversed(diff["commits"]))[:max_commits]
                result["truncated"] = diff["truncated"] or len(diff["commits"]) > max_commits
        attempt("unreleased", probe_unreleased)
    return result


# --------------------------------------------------------------------------- #
# local probes
# --------------------------------------------------------------------------- #

AHEAD_COUNT = re.compile(r"ahead (\d+)")


def dirty_count(path):
    return len((git(path, "status", "--porcelain", check=False) or "").splitlines())


def unpushed_work(path, branch, upstream, track):
    """Commits on `branch` that no remote holds, or None when it is published."""
    if upstream and "[gone]" not in track:
        match = AHEAD_COUNT.search(track)
        if not match:
            return None
        return {"branch": branch, "count": int(match.group(1)),
                "kind": "ahead", "upstream": upstream}
    # With no upstream to compare against, the branch's own commits are the
    # measure: what is reachable from it and from no remote-tracking ref.
    count = git(path, "rev-list", "--count", branch, "--not", "--remotes", check=False)
    if not count or not int(count):
        return None
    return {"branch": branch, "count": int(count),
            "kind": "gone" if upstream else "untracked", "upstream": upstream or None}


def probe_clone(clone, default_branch, fetch):
    info = dict(clone)
    info.update({
        "fetch_error": None,
        "branch": None,
        "detached": False,
        "dirty": 0,
        "ahead": 0,
        "behind": 0,
        "upstream": None,
        "default": default_branch,
        "default_ahead": 0,
        "default_behind": 0,
        "merged_branches": [],
        "gone_branches": [],
        "unpushed": [],
    })
    path = clone["path"]

    if fetch:
        proc = subprocess.run(["git", "-C", path, "fetch", "--prune", "--quiet", "origin"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            info["fetch_error"] = first_line(proc.stderr) or f"git fetch exited {proc.returncode}"

    head = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    info["detached"] = head == "HEAD"
    info["branch"] = None if info["detached"] else head
    info["dirty"] = dirty_count(path)

    if not info["default"]:
        symbolic = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
        if symbolic and symbolic.startswith("origin/"):
            info["default"] = symbolic[len("origin/"):]

    if info["branch"]:
        upstream = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                       info["branch"] + "@{u}", check=False)
        info["upstream"] = upstream
        if upstream:
            counts = git(path, "rev-list", "--left-right", "--count",
                         f"{info['branch']}...{upstream}", check=False)
            if counts:
                ahead, behind = counts.split()
                info["ahead"], info["behind"] = int(ahead), int(behind)

    default = info["default"]
    default_ref = f"origin/{default}" if default else None
    have_default = default_ref and git(path, "rev-parse", "--verify", "--quiet",
                                       default_ref, check=False) is not None

    if have_default and default != info["branch"]:
        counts = git(path, "rev-list", "--left-right", "--count",
                     f"{default}...{default_ref}", check=False)
        if counts:
            ahead, behind = counts.split()
            info["default_ahead"], info["default_behind"] = int(ahead), int(behind)

    for line in (git(path, "for-each-ref",
                     "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
                     "refs/heads/", check=False) or "").splitlines():
        name, upstream, track = (line.split("\t") + ["", ""])[:3]
        if "[gone]" in track and name not in (default, info["branch"]):
            info["gone_branches"].append(name)
        stranded = unpushed_work(path, name, upstream, track)
        if stranded:
            info["unpushed"].append(stranded)

    if have_default:
        for line in (git(path, "for-each-ref", "--merged", default_ref,
                         "--format=%(refname:short)", "refs/heads/", check=False) or "").splitlines():
            if line and line not in (default, info["branch"]) and line not in info["gone_branches"]:
                info["merged_branches"].append(line)

    return info


def branch_merged_remotely(path, default, branch):
    """True when origin/<branch> is already an ancestor of origin/<default>."""
    if not default:
        return False
    proc = subprocess.run(
        ["git", "-C", path, "merge-base", "--is-ancestor",
         f"origin/{branch}", f"origin/{default}"],
        capture_output=True, text=True)
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

TTY = sys.stdout.isatty()


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else text


def link(label, url):
    """OSC 8 hyperlink; falls back to the bare URL when output isn't a terminal."""
    if not url:
        return label
    if not TTY:
        return f"{label}  {url}"
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def name_link(label, url):
    """Link a project name to the page its section is about.

    Terminal only: a plain dump already carries the URL of whatever detail sits
    beside the name, and a second one there reads as part of the first."""
    return link(label, url) if TTY else label


def clone_url(clone, path=""):
    """The GitHub page for a clone's origin, keyed off the parsed owner/name.

    The directory name can't be trusted for this — relocating a clone onto its
    own path is the very thing `reconcile` exists to fix."""
    if not (clone["owner"] and clone["name"]):
        return None
    return f"https://github.com/{clone['owner']}/{clone['name']}{path}"


def fmt_date(stamp):
    return stamp[:10] if stamp else "-"


def parse_stamp(stamp):
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def age_days(stamp):
    moment = parse_stamp(stamp)
    if not moment:
        return None
    return (datetime.now(timezone.utc) - moment).days


def heading(title, subtitle=None):
    print()
    print(paint(title, "1"))
    if subtitle:
        print(subtitle)


def confirm(question, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"    skipped, needs a terminal to confirm: {question}")
        return False
    try:
        return input(f"    {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

def collect_reconcile(repos, held, clones, root, owner, jobs):
    """Clone tree vs owned repos, classified. Reads only; writes nothing."""
    owned = {r["name"].lower(): r for r in repos}
    held_by_name = {r["name"].lower(): why for r, why in held}
    mine = [c for c in clones if c["owner"] and c["owner"].lower() == owner.lower()]
    foreign = [c for c in clones if c not in mine]
    cloned = {c["name"].lower() for c in mine if c["name"]}

    missing = [r for r in repos if r["name"].lower() not in cloned]
    unmatched = [c for c in mine if c["name"]
                 and c["name"].lower() not in owned
                 and c["name"].lower() not in held_by_name]

    # A clone whose origin is in neither list is either a repo that was deleted
    # or renamed, or one the account listing never reached.
    def classify(clone):
        full = f"{clone['owner']}/{clone['name']}"
        try:
            info = gh_json(f"repos/{full}")
        except GhError as err:
            if "Not Found" in str(err) or "404" in str(err):
                return clone, "gone", None
            return clone, "error", str(err)
        moved = info["full_name"].lower() != full.lower()
        return clone, "moved" if moved else "outside", info

    verdicts = []
    if unmatched:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            verdicts = list(pool.map(classify, unmatched))

    gone = [(c, dirty_count(c["path"])) for c, kind, _ in verdicts if kind == "gone"]
    moved = [(c, i) for c, kind, i in verdicts if kind == "moved"]
    outside = [(c, i) for c, kind, i in verdicts if kind == "outside"]
    failed = [(c, i) for c, kind, i in verdicts if kind == "error"]

    # Every clone belongs at <root>/<repo name>. A clone nested under a grouping
    # directory, or filed under an upstream's name when the fork is your own,
    # is the finding — the layout is what makes the two agree at a glance.
    canonical_names = {r["name"].lower(): r["name"] for r in repos}
    canonical_names.update({r["name"].lower(): r["name"] for r, _ in held})
    canonical_names.update({i["name"].lower(): i["name"] for _, i in outside})
    misplaced = []
    for clone in mine:
        canonical = canonical_names.get((clone["name"] or "").lower())
        if not canonical:
            continue
        leaf = os.path.basename(clone["rel"])
        if os.path.dirname(clone["rel"]) or leaf.lower() != canonical.lower():
            misplaced.append((clone, canonical))

    held_rows = [{"name": r["name"], "url": r["url"], "why": why,
                  "cloned": r["name"].lower() in cloned} for r, why in held]
    held_rows += [{"name": f"{c['owner']}/{c['name']}", "url": i["html_url"],
                   "why": "outside the account listing", "cloned": True}
                  for c, i in outside]
    held_rows.sort(key=lambda h: (h["why"], h["name"].lower()))

    return {
        "root": root,
        "owner": owner,
        "owned": len(repos),
        "missing": sorted(missing, key=lambda r: r["name"].lower()),
        "gone": sorted(gone, key=lambda pair: pair[0]["rel"].lower()),
        "misplaced": sorted(misplaced, key=lambda pair: pair[0]["rel"].lower()),
        "moved": sorted(moved, key=lambda pair: pair[0]["rel"].lower()),
        "held": held_rows,
        "failed": failed,
        "foreign": sorted(foreign, key=lambda c: c["rel"].lower()),
    }


def section_reconcile(report, clones, fix, assume_yes):
    """Print the reconcile findings. Returns the clones that survive the pass."""
    root, owner = report["root"], report["owner"]
    heading("RECONCILE", f"{root} vs the {report['owned']} repo(s) in scope")

    missing, gone = report["missing"], report["gone"]
    misplaced, moved = report["misplaced"], report["moved"]
    failed, foreign = report["failed"], report["foreign"]

    clean = True

    if missing:
        clean = False
        print()
        print(paint(f"  on GitHub, not cloned ({len(missing)})", "1"))
        for repo in missing:
            flags = ", ".join(f for f, on in (("archived", repo["isArchived"]),
                                              ("fork", repo["isFork"]),
                                              ("private", repo["isPrivate"])) if on)
            label = f"    {repo['name']}" + (f"  [{flags}]" if flags else "")
            print(link(label, repo["url"]))
            target = os.path.join(root, repo["name"])
            if fix:
                print(f"      cloning into {target}")
                proc = subprocess.run(["git", "clone", repo["sshUrl"], target],
                                      capture_output=True, text=True)
                if proc.returncode != 0:
                    print(paint(f"      clone failed: {proc.stderr.strip().splitlines()[-1]}", "31"))
                else:
                    clones.append({"path": target, "rel": repo["name"],
                                   "origin": repo["sshUrl"], "owner": owner,
                                   "name": repo["name"]})
            else:
                print(f"      git clone {repo['sshUrl']} {target}")

    if gone:
        clean = False
        print()
        print(paint(f"  cloned, no longer on GitHub ({len(gone)})", "1"))
        for clone, dirty in gone:
            note = f"  [{dirty} uncommitted change(s)]" if dirty else ""
            print(f"    {clone['rel']}  ->  {clone['owner']}/{clone['name']} (404){note}")
            if fix:
                if confirm(f"delete {clone['path']}?", assume_yes):
                    shutil.rmtree(clone["path"])
                    clones.remove(clone)
                    print(f"      deleted {clone['path']}")
            else:
                print(f"      rm -rf {clone['path']}")

    if misplaced:
        clean = False
        print()
        print(paint(f"  cloned somewhere other than {root}/<repo> ({len(misplaced)})", "1"))
        for clone, canonical in misplaced:
            target = os.path.join(root, canonical)
            landing = name_link(canonical, f"https://github.com/{owner}/{canonical}")
            print(f"    {clone['rel']}  ->  {landing}")
            if os.path.exists(target):
                print(paint(f"      {target} is already taken — move that aside first", "31"))
                continue
            if fix:
                if confirm(f"move {clone['path']} to {target}?", assume_yes):
                    parent = os.path.dirname(clone["path"])
                    shutil.move(clone["path"], target)
                    clone["path"], clone["rel"] = target, canonical
                    print(f"      moved to {target}")
                    prune_empty(parent, root)
            else:
                print(f"      mv {clone['path']} {target}")

    if moved:
        clean = False
        print()
        print(paint(f"  cloned under an old name ({len(moved)})", "1"))
        for clone, info in moved:
            print(f"    {clone['rel']}  ->  renamed to "
                  + name_link(info["full_name"], info["html_url"]))
            print(f"      git -C {clone['path']} remote set-url origin {info['ssh_url']}")

    if failed:
        clean = False
        print()
        print(paint(f"  could not be checked ({len(failed)})", "1"))
        for clone, err in failed:
            print(f"    {clone['rel']}: {err}")

    if foreign:
        print()
        print(f"  clones of other owners ({len(foreign)}), reported on local state only:")
        for clone in foreign:
            print(f"    {clone['rel']}  ->  {clone['origin'] or 'no origin remote'}")

    if clean:
        print()
        print("  in sync")

    return clones


def collect_uncommitted(states, all_details):
    """Clones holding work no remote has, dirtiest tree first."""
    rows = []
    for s in states:
        pending = sum(u["count"] for u in s["unpushed"])
        if s["dirty"] or pending or all_details:
            rows.append((s, pending))
    rows.sort(key=lambda pair: (-pair[0]["dirty"], -pair[1], pair[0]["rel"].lower()))
    return rows


def section_uncommitted(rows, states):
    heading("UNCOMMITTED AND UNPUSHED WORK",
            "work held by one clone and nothing else; dirtiest tree first")
    if not rows:
        print()
        print(f"  every one of {len(states)} clone(s) is committed and pushed")
        return

    for s, pending in rows:
        print()
        print(paint(name_link(f"  {s['rel']}", clone_url(s, "/branches")), "1"))
        if not s["dirty"] and not pending:
            print("    committed and pushed")
            continue
        if s["dirty"]:
            print(f"    {s['dirty']} uncommitted change(s) on "
                  f"{s['branch'] or 'a detached HEAD'}")
            print(f"      cd {s['path']}")
        for u in sorted(s["unpushed"], key=lambda u: (-u["count"], u["branch"])):
            where = {"ahead": f"not pushed to {u['upstream']}",
                     "gone": f"on no remote ({u['upstream']} was deleted)",
                     "untracked": "on no remote (no upstream)"}[u["kind"]]
            print(f"    {u['branch']}: {u['count']} commit(s) {where}")
            flag = [] if u["kind"] == "ahead" else ["-u"]
            print("      " + " ".join(["git", "-C", s["path"], "push",
                                       *flag, "origin", u["branch"]]))


def collect_local_branches(states):
    """Per clone, the branches that have served their purpose — and the ones held back."""
    rows = []
    for s in sorted(states, key=lambda s: s["rel"].lower()):
        merged, gone = s["merged_branches"], s["gone_branches"]
        if not merged and not gone:
            continue
        # A branch whose remote is gone can still hold the only copy of its
        # commits, and `branch -D` would take them with it.
        stranded = {u["branch"]: u["count"] for u in s["unpushed"]}
        held = [(b, stranded[b]) for b in sorted(gone) if b in stranded]
        gone = sorted(b for b in gone if b not in stranded)
        merged = sorted(merged)
        rows.append({"state": s, "merged": merged, "gone": gone, "held": held,
                     "branches": merged + gone})
    return rows


def section_local_branches(rows, fix, assume_yes):
    heading("LOCAL BRANCHES TO CLEAN UP",
            "fully merged into the default branch, or tracking a deleted remote")
    for row in rows:
        s, merged, gone = row["state"], row["merged"], row["gone"]
        print()
        named = name_link(s["rel"], clone_url(s, "/branches"))
        print(paint(f"  {named}  ({len(merged) + len(gone)})", "1"))
        if merged:
            print(f"    merged into {s['default']} ({len(merged)}): " + ", ".join(merged))
        if gone:
            print(f"    upstream deleted ({len(gone)}): " + ", ".join(gone))
        for branch, count in row["held"]:
            print(f"    holding back {branch}: upstream deleted, but it still "
                  f"carries {count} commit(s) no remote has")
        branches = row["branches"]
        if not branches:
            continue
        command = ["git", "-C", s["path"], "branch", "-D", *branches]
        if fix:
            if confirm(f"delete {len(branches)} local branch(es) in {s['rel']}?", assume_yes):
                proc = subprocess.run(command, capture_output=True, text=True)
                stream = proc.stdout if proc.returncode == 0 else proc.stderr
                print(f"      {len(stream.splitlines())} deleted"
                      if proc.returncode == 0 else
                      paint(f"      delete failed: {first_line(stream)}", "31"))
        else:
            print("      " + " ".join(command))
    if not rows:
        print()
        print("  nothing to clean up")


def collect_orphan_branches(results, clone_by_repo, stale_days):
    """Remote branches with no open PR that have gone stale, oldest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    rows = []
    for r in sorted(results, key=lambda r: r["repo"].lower()):
        failed = {a: m for a, m in r["errors"].items() if a in ("branches", "pulls")}
        if failed:
            rows.append({"repo": r, "failed": failed, "orphans": [], "names": []})
            continue
        open_heads = {p["head"] for p in r["pulls"]}
        orphans = []
        for branch in r["branches"]:
            if branch["name"] == r["branch"] or branch["name"] in open_heads:
                continue
            moment = parse_stamp(branch["date"])
            if moment and moment > cutoff:
                continue
            orphans.append(branch)
        if not orphans:
            continue
        clone = clone_by_repo.get(r["repo"].lower())
        orphans.sort(key=lambda b: b["date"] or "")
        for branch in orphans:
            branch["merged"] = branch_merged_remotely(
                clone["path"], r["branch"], branch["name"]) if clone else None
            branch["age_days"] = age_days(branch["date"])
        rows.append({"repo": r, "failed": {}, "orphans": orphans,
                     "names": [b["name"] for b in orphans]})
    return rows


def section_orphan_branches(rows, stale_days, fix, assume_yes):
    heading("ORPHANED REMOTE BRANCHES",
            f"no open PR and no commit in {stale_days} day(s)")
    for row in rows:
        r = row["repo"]
        if row["failed"]:
            print()
            for area, message in sorted(row["failed"].items()):
                print(paint(f"  {r['repo']}: {area}: {message}", "31"))
            continue
        print()
        print(paint(name_link(f"  {r['repo']}", f"{r['url']}/branches"), "1"))
        if r["branches_truncated"]:
            print("    note: more than 100 branches; only the first 100 were checked")
        for branch in row["orphans"]:
            merged = "" if branch["merged"] is None else (
                "  [merged into " + r["branch"] + "]" if branch["merged"] else "  [not merged]")
            days = branch["age_days"]
            aged = f"{days}d" if days is not None else "unknown age"
            label = f"    {branch['name']}  {branch['sha']}  {fmt_date(branch['date'])} ({aged}){merged}"
            print(link(label, f"{r['url']}/tree/{branch['name']}"))
        names = row["names"]
        command = ["git", "push", f"git@github.com:{r['repo']}.git", "--delete", *names]
        if fix:
            if confirm(f"delete {len(names)} remote branch(es) in {r['repo']}?", assume_yes):
                proc = subprocess.run(command, capture_output=True, text=True)
                if proc.returncode == 0:
                    print(f"      deleted {len(names)} branch(es)")
                else:
                    print(paint(f"      delete failed: {first_line(proc.stderr)}", "31"))
        else:
            print("      " + " ".join(command))
    if not rows:
        print()
        print("  none")


def unreleased_failure(r):
    return r["errors"].get("unreleased")


def collect_unreleased(results, all_details):
    """Released projects with commits since their marker, longest-waiting first."""
    released = [r for r in results if r["release"] or unreleased_failure(r)]
    ordered = sorted(released,
                     key=lambda r: (unreleased_failure(r) is not None, r["oldest"] or "~"))
    return [r for r in ordered if all_details or r["ahead"] or unreleased_failure(r)]


def section_unreleased(detailed, max_commits):
    heading("UNRELEASED CHANGES",
            "longest-waiting project first; commits newest first\n"
            "counts exclude the version-bump commit of the current release "
            "(--include-release-commits to count it)")
    failure = unreleased_failure
    if not detailed:
        print()
        print("  every released project is up to date")
        return
    for r in detailed:
        rel = r["release"]
        print()
        if failure(r):
            print(paint(f"  {r['repo']}: {failure(r)}", "31"))
            continue
        qualifier = ", tag only" if rel["kind"] == "tag" else (
            ", prerelease" if rel["prerelease"] else "")
        span = rel["tag"] + "..." + r["branch"]
        named = name_link(r["repo"], f"{r['url']}/releases")
        pending = link(f"{r['ahead']} commit(s) since {rel['tag']}"
                       f" ({fmt_date(rel['published'])}{qualifier})",
                       f"{r['url']}/compare/{span}")
        print(paint(f"  {named}: {pending}", "1"))
        if r["behind"]:
            print(f"    note: {rel['tag']} is {r['behind']} commit(s) ahead of "
                  f"{r['branch']} (tagged off-branch?)")
        for c in r["commits"]:
            label = f"{c['sha']}  {fmt_date(c['date'])}  {c['subject']}"
            line = "    " + link(label, f"{r['url']}/commit/{c['sha']}")
            if c.get("release_commit"):
                line = paint(f"{line}  [release commit, not pending]", "2")
            print(line)
        if r["truncated"]:
            print(f"    … older commits not shown (newest {max_commits}; raise with --max-commits)")


def collect_prs(results):
    """Every open pull request across the account, oldest first."""
    broken = sorted((r for r in results if "pulls" in r["errors"]),
                    key=lambda r: r["repo"].lower())
    rows = [(r, p) for r in results for p in r["pulls"]]
    rows.sort(key=lambda pair: pair[1]["created"] or "")
    return rows, broken


def section_prs(rows, broken):
    heading("OPEN PULL REQUESTS", "oldest first")
    if not rows and not broken:
        print()
        print("  none")
        return
    print()
    for r in broken:
        print(paint(f"  {r['repo']}: could not list pull requests: "
                    f"{r['errors']['pulls']}", "31"))
    for r, pull in rows:
        p = pull
        days = age_days(p["created"])
        flags = " [draft]" if p["draft"] else ""
        named = name_link(r["repo"], f"{r['url']}/pulls")
        print(f"  {named}" + link(f"#{p['number']}{flags}  {p['title']}", p["url"]))
        print(f"      {p['head']} -> {p['base']}  by {p['author']}  "
              f"opened {fmt_date(p['created'])} ({days}d)")


NO_MILESTONE = "(no milestone)"


def collect_issues(results):
    """Open issues grouped by milestone, soonest due date first."""
    broken = sorted((r for r in results if "issues" in r["errors"]),
                    key=lambda r: r["repo"].lower())
    groups = {}
    for r in results:
        for issue in r["issues"]:
            key = issue["milestone"] or NO_MILESTONE
            groups.setdefault(key, {"due": issue["milestone_due"], "items": []})
            groups[key]["items"].append((r, issue))

    def order(item):
        title, group = item
        if title == NO_MILESTONE:
            return (2, "", title)
        return (0 if group["due"] else 1, group["due"] or "", title)

    ordered = []
    for title, group in sorted(groups.items(), key=order):
        group["title"] = title
        group["items"].sort(key=lambda pair: pair[1]["created"] or "")
        ordered.append(group)
    return ordered, broken


def section_issues(groups, broken):
    heading("OPEN ISSUES", "grouped by milestone, soonest due date first")
    if not groups and not broken:
        print()
        print("  none")
        return
    if broken:
        print()
        for r in broken:
            print(paint(f"  {r['repo']}: could not list issues: "
                        f"{r['errors']['issues']}", "31"))

    for group in groups:
        title = group["title"]
        due = f"  (due {fmt_date(group['due'])})" if group["due"] else ""
        print()
        print(paint(f"  {title}{due}  — {len(group['items'])} issue(s)", "1"))
        for r, issue in group["items"]:
            labels = f"  [{', '.join(issue['labels'])}]" if issue["labels"] else ""
            named = name_link(r["repo"], f"{r['url']}/issues")
            print(f"    {named}"
                  + link(f"#{issue['number']}  {issue['title']}{labels}", issue["url"]))


def collect_behind(states, all_details):
    """Clones trailing origin, each with the notes that say how."""
    rows = []
    for s in sorted(states, key=lambda s: s["rel"].lower()):
        notes = []
        if s["fetch_error"]:
            notes.append({"text": f"fetch failed: {s['fetch_error']}", "tone": "risk"})
        if s["detached"]:
            notes.append({"text": "detached HEAD", "tone": None})
        elif not s["upstream"]:
            notes.append({"text": "no upstream", "tone": None})
        if s["behind"]:
            notes.append({"text": f"{s['behind']} behind", "tone": "warn"})
        if s["default_behind"]:
            notes.append({"text": f"{s['default']} is {s['default_behind']} behind origin",
                          "tone": "warn"})
        if not notes and not all_details:
            continue
        rows.append((s, notes or [{"text": "up to date", "tone": "ok"}]))
    return rows


def section_behind(rows, states, fix):
    heading("CLONES BEHIND ORIGIN",
            "the checked-out branch against its upstream, and the default branch "
            "against origin\n--fix fast-forwards each one that is clean")
    if not rows:
        print()
        print(f"  all {len(states)} clone(s) are level with origin")
        return

    width = max(len(s["rel"]) for s, _ in rows)
    print()
    for s, notes in rows:
        branch = s["branch"] or "(detached)"
        rendered = [paint(n["text"], "31") if n["tone"] == "risk" else n["text"]
                    for n in notes]
        named = name_link(s["rel"],
                          clone_url(s, f"/commits/{s['branch']}" if s["branch"] else ""))
        print(f"  {named}{' ' * (width - len(s['rel']))}  {branch:<24}  "
              + "; ".join(rendered))
        if not fix:
            continue
        if s["behind"] and not s["ahead"] and not s["dirty"] and s["upstream"]:
            proc = subprocess.run(["git", "-C", s["path"], "merge", "--ff-only", s["upstream"]],
                                  capture_output=True, text=True)
            if proc.returncode == 0:
                print(f"      fast-forwarded {branch} to {s['upstream']}")
                s["behind"] = 0
            else:
                print(paint(f"      fast-forward failed: {first_line(proc.stderr)}", "31"))
        # The default branch isn't checked out here, so a merge can't reach it;
        # a refspec fetch moves the ref and refuses anything but a fast-forward.
        if s["default_behind"] and not s["default_ahead"]:
            default = s["default"]
            proc = subprocess.run(
                ["git", "-C", s["path"], "fetch", "origin", f"{default}:{default}"],
                capture_output=True, text=True)
            if proc.returncode == 0:
                print(f"      fast-forwarded {default} to origin/{default}")
                s["default_behind"] = 0
            else:
                print(paint(f"      {default} update failed: {first_line(proc.stderr)}", "31"))


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #

# Every section reduces to the same three levels — group, item, step — so one
# renderer draws all eight. A step is a line of detail and, where the terminal
# report would print a paste-ready command under it, that command.

def plural(count, word, many=None):
    return f"{count} {word if count == 1 else (many or word + 's')}"


def cell(text, cls=None):
    return {"text": text, "cls": cls}


def step(text=None, command=None, url=None, tone=None, cells=None, advisory=False):
    """A line of detail and, where the report would print one, its command.

    An advisory command takes you to the work rather than settling it, so it
    stays out of the copy-everything list while staying copyable on its own."""
    return {"text": text, "command": command, "url": url, "tone": tone,
            "cells": list(cells) if cells else None, "advisory": advisory}


def item(name, url=None, meta=None, tags=(), steps=(), command=None, tone=None):
    return {"name": name, "url": url, "meta": meta, "tags": list(tags),
            "steps": list(steps), "command": command, "tone": tone}


def group(label, items, tone=None, note=None, info=False, dense=False):
    """`info` marks a group that reports context rather than work to be done,
    so it stays out of every count the page presents as a finding."""
    return {"label": label, "tone": tone, "note": note, "info": info,
            "dense": dense, "items": list(items)}


def page_reconcile(report, root):
    groups = []
    if report["missing"]:
        groups.append(group("On GitHub, not cloned", [
            item(repo["name"], url=repo["url"],
                 tags=[f for f, on in (("archived", repo["isArchived"]),
                                       ("fork", repo["isFork"]),
                                       ("private", repo["isPrivate"])) if on],
                 command=f"git clone {repo['sshUrl']} {os.path.join(root, repo['name'])}")
            for repo in report["missing"]], tone="warn"))
    if report["gone"]:
        groups.append(group("Cloned, no longer on GitHub", [
            item(clone["rel"], meta=f"{clone['owner']}/{clone['name']} (404)",
                 tone="risk" if dirty else None,
                 steps=[step(f"{plural(dirty, 'uncommitted change')} would go with it",
                             tone="risk")] if dirty else [],
                 command=f"rm -rf {clone['path']}")
            for clone, dirty in report["gone"]], tone="risk"))
    if report["misplaced"]:
        items = []
        for clone, canonical in report["misplaced"]:
            target = os.path.join(root, canonical)
            taken = os.path.exists(target)
            items.append(item(
                clone["rel"], meta=f"belongs at {canonical}",
                tone="risk" if taken else None,
                steps=[step(f"{target} is already taken — move that aside first",
                            tone="risk")] if taken else [],
                command=None if taken else f"mv {clone['path']} {target}"))
        groups.append(group(f"Cloned somewhere other than {root}/<repo>", items, tone="warn"))
    if report["moved"]:
        groups.append(group("Cloned under an old name", [
            item(clone["rel"], meta=f"renamed to {info['full_name']}",
                 command=f"git -C {clone['path']} remote set-url origin {info['ssh_url']}")
            for clone, info in report["moved"]], tone="warn"))
    if report["failed"]:
        groups.append(group("Could not be checked", [
            item(clone["rel"], steps=[step(err, tone="risk")])
            for clone, err in report["failed"]], tone="risk"))
    if report["held"]:
        groups.append(group(
            "Held back from this run",
            [item(h["name"], url=h["url"],
                  tags=[h["why"]] + (["cloned"] if h["cloned"] else []))
             for h in report["held"]],
            note="Widen the run with --include-forks, --include-archived, or --no-ignore.",
            info=True, dense=True))
    if report["foreign"]:
        groups.append(group(
            "Clones of other owners",
            [item(clone["rel"], meta=clone["origin"] or "no origin remote")
             for clone in report["foreign"]],
            note="Reported on local state only.", info=True))
    return groups, "In sync"


UNPUSHED_WHERE = {
    "ahead": "not pushed to {upstream}",
    "gone": "on no remote ({upstream} was deleted)",
    "untracked": "on no remote (no upstream)",
}


def page_uncommitted(rows):
    items = []
    for state, pending in rows:
        steps = []
        if state["dirty"]:
            steps.append(step(cells=[
                cell("working tree", "key mono"),
                cell(f"on {state['branch'] or 'a detached HEAD'}", "what")],
                command=f"cd {state['path']}", advisory=True))
        for u in sorted(state["unpushed"], key=lambda u: (-u["count"], u["branch"])):
            where = UNPUSHED_WHERE[u["kind"]].format(upstream=u["upstream"])
            flag = [] if u["kind"] == "ahead" else ["-u"]
            steps.append(step(cells=[
                cell(u["branch"], "key mono"),
                cell(f"{plural(u['count'], 'commit')} {where}", "what")],
                command=" ".join(["git", "-C", state["path"], "push",
                                  *flag, "origin", u["branch"]])))
        stake = []
        if state["dirty"]:
            stake.append(plural(state["dirty"], "uncommitted change"))
        if pending:
            stake.append(plural(pending, "unpushed commit"))
        items.append(item(
            state["rel"], meta=" · ".join(stake) or "committed and pushed",
            tone="risk" if stake else "ok",
            steps=steps or [step("Committed and pushed", tone="ok")]))
    return ([group("Held by one clone and nothing else", items, tone="risk")] if items
            else []), "Every clone is committed and pushed"


def page_local_branches(rows):
    items = []
    for row in rows:
        state = row["state"]
        steps = []
        if row["merged"]:
            steps.append(step(cells=[
                cell(f"merged into {state['default']}", "key"),
                cell(", ".join(row["merged"]), "what mono")]))
        if row["gone"]:
            steps.append(step(cells=[
                cell("upstream deleted", "key"),
                cell(", ".join(row["gone"]), "what mono")]))
        for branch, count in row["held"]:
            steps.append(step(cells=[
                cell("held back", "key warn"),
                cell(branch, "what mono"),
                cell(f"{plural(count, 'commit')} no remote has", "when")]))
        held_note = plural(len(row["held"]), "branch", "branches") + " held back"
        items.append(item(
            state["rel"],
            meta=plural(len(row["branches"]), "branch", "branches") + " to delete"
            if row["branches"] else held_note,
            steps=steps,
            command=" ".join(["git", "-C", state["path"], "branch", "-D", *row["branches"]])
            if row["branches"] else None))
    return ([group("Served their purpose", items, tone="warn")] if items else
            []), "Nothing to clean up"


def page_orphan_branches(rows):
    items, broken = [], []
    for row in rows:
        r = row["repo"]
        if row["failed"]:
            broken.append(item(r["repo"], steps=[
                step(f"{area}: {message}", tone="risk")
                for area, message in sorted(row["failed"].items())]))
            continue
        steps = []
        for branch in row["orphans"]:
            days = branch["age_days"]
            cells = [cell(branch["name"], "key mono"),
                     cell(branch["sha"], "mono dim"),
                     cell(f"{fmt_date(branch['date'])}"
                          + (f" ({days}d)" if days is not None else ""), "when")]
            if branch["merged"] is not None:
                cells.append(cell(f"merged into {r['branch']}" if branch["merged"]
                                  else "not merged",
                                  "what" if branch["merged"] else "what warn"))
            steps.append(step(cells=cells, url=f"{r['url']}/tree/{branch['name']}"))
        if r["branches_truncated"]:
            steps.append(step("More than 100 branches; only the first 100 were checked",
                              tone="mute"))
        items.append(item(
            r["repo"], url=r["url"],
            meta=plural(len(row["orphans"]), "branch", "branches") + " with no open PR",
            steps=steps,
            command=" ".join(["git", "push", f"git@github.com:{r['repo']}.git",
                              "--delete", *row["names"]])))
    groups = []
    if items:
        groups.append(group("No open PR, no recent commit", items, tone="warn"))
    if broken:
        groups.append(group("Could not be listed", broken, tone="risk"))
    return groups, "None"


def page_unreleased(detailed, max_commits):
    items, broken = [], []
    for r in detailed:
        failure = unreleased_failure(r)
        if failure:
            broken.append(item(r["repo"], url=r["url"], steps=[step(failure, tone="risk")]))
            continue
        rel = r["release"]
        qualifier = ", tag only" if rel["kind"] == "tag" else (
            ", prerelease" if rel["prerelease"] else "")
        steps = []
        if r["behind"]:
            steps.append(step(
                f"{rel['tag']} is {plural(r['behind'], 'commit')} ahead of "
                f"{r['branch']} — tagged off-branch?", tone="warn"))
        for c in r["commits"]:
            cells = [cell(c["sha"], "key mono"),
                     cell(fmt_date(c["date"]), "when"),
                     cell(c["subject"], "what")]
            if c.get("release_commit"):
                cells.append(cell("release commit, not pending", "what dim"))
            steps.append(step(cells=cells, url=f"{r['url']}/commit/{c['sha']}"))
        if r["truncated"]:
            steps.append(step(f"Older commits not shown — newest {max_commits}; "
                              f"raise with --max-commits", tone="mute"))
        items.append(item(
            r["repo"],
            url=f"{r['url']}/compare/{rel['tag']}...{r['branch']}",
            meta=f"{plural(r['ahead'], 'commit')} since {rel['tag']}"
                 f" ({fmt_date(rel['published'])}{qualifier})",
            tone="warn" if r["ahead"] else "ok", steps=steps))
    groups = []
    if items:
        groups.append(group("Waiting on a release, longest first", items, tone="warn"))
    if broken:
        groups.append(group("Could not be read", broken, tone="risk"))
    return groups, "Every released project is up to date"


def page_prs(rows, broken):
    items = [item(f"{r['repo']}#{p['number']}", url=p["url"], meta=p["title"],
                  tags=["draft"] if p["draft"] else [],
                  steps=[step(cells=[
                      cell(f"{p['head']} → {p['base']}", "mono dim"),
                      cell(f"by {p['author']}", "what"),
                      cell(f"opened {fmt_date(p['created'])} "
                           f"({age_days(p['created'])}d)", "when")])])
             for r, p in rows]
    groups = []
    if items:
        groups.append(group("Open, oldest first", items))
    if broken:
        groups.append(group("Could not be listed", [
            item(r["repo"], steps=[step(r["errors"]["pulls"], tone="risk")])
            for r in broken], tone="risk"))
    return groups, "None"


def page_issues(groups_in, broken):
    groups = []
    for entry in groups_in:
        due = f"due {fmt_date(entry['due'])}" if entry["due"] else None
        groups.append(group(
            entry["title"], [
                item(f"{r['repo']}#{issue['number']}", url=issue["url"],
                     meta=issue["title"], tags=issue["labels"])
                for r, issue in entry["items"]],
            note=due))
    if broken:
        groups.append(group("Could not be listed", [
            item(r["repo"], steps=[step(r["errors"]["issues"], tone="risk")])
            for r in broken], tone="risk"))
    return groups, "None"


def page_behind(rows):
    items = []
    for state, notes in rows:
        tone = "risk" if any(n["tone"] == "risk" for n in notes) else (
            "warn" if any(n["tone"] == "warn" for n in notes) else "ok")
        items.append(item(
            state["rel"], meta=state["branch"] or "(detached)", tone=tone,
            steps=[step(n["text"], tone=n["tone"]) for n in notes]))
    return ([group("The checked-out branch, and the default branch", items)]
            if items else []), "Every clone is level with origin"


# What a section counts, so a tally reads as the thing itself rather than as a
# uniform "finding" — 87 open issues are a backlog, not 87 problems.
SECTION_UNITS = {
    "reconcile": ("finding", "findings"),
    "uncommitted": ("clone at risk", "clones at risk"),
    "local-branches": ("clone to tidy", "clones to tidy"),
    "orphan-branches": ("repo with stale branches", "repos with stale branches"),
    "prs": ("open", "open"),
    "unreleased": ("project waiting", "projects waiting"),
    "issues": ("open", "open"),
    "behind": ("clone behind", "clones behind"),
}

SECTION_TITLES = {
    "reconcile": ("Reconcile",
                  "The clone tree against the repos you own. Every clone belongs "
                  "at <root>/<repo name>, flat."),
    "uncommitted": ("Uncommitted and unpushed",
                    "Work held by one clone and nothing else — the only section "
                    "about what a dead disk would take with it."),
    "local-branches": ("Local branches to clean up",
                       "Merged into the default branch, or tracking a remote that "
                       "is gone. A branch still carrying unpushed commits is held back."),
    "orphan-branches": ("Orphaned remote branches",
                        "No open pull request, and no commit in the staleness window."),
    "prs": ("Open pull requests", "Oldest first."),
    "unreleased": ("Unreleased changes",
                   "Commits on the default branch since the last release or tag, "
                   "longest-waiting project first."),
    "issues": ("Open issues", "Grouped by milestone, soonest due date first."),
    "behind": ("Clones behind origin",
               "The checked-out branch against its upstream, and the default "
               "branch against origin."),
}


def build_payload(found, wanted, owner, root, states, results, args):
    builders = {
        "reconcile": lambda: page_reconcile(found["reconcile"], root),
        "uncommitted": lambda: page_uncommitted(found["uncommitted"]),
        "local-branches": lambda: page_local_branches(found["local-branches"]),
        "orphan-branches": lambda: page_orphan_branches(found["orphan-branches"]),
        "prs": lambda: page_prs(*found["prs"]),
        "unreleased": lambda: page_unreleased(found["unreleased"], args.max_commits),
        "issues": lambda: page_issues(*found["issues"]),
        "behind": lambda: page_behind(found["behind"]),
    }
    sections = []
    for name in SECTIONS:
        title, blurb = SECTION_TITLES[name]
        if name not in wanted:
            sections.append({"id": name, "title": title, "blurb": blurb,
                             "skipped": True, "groups": [], "clean": None,
                             "unit": list(SECTION_UNITS[name])})
            continue
        groups, clean = builders[name]()
        one, many = SECTION_UNITS[name]
        sections.append({"id": name, "title": title, "blurb": blurb,
                         "skipped": False, "groups": groups, "clean": clean,
                         "unit": [one, many]})
    return {
        "owner": owner,
        "root": root,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repos": len(results),
        "clones": len(states),
        "stale_days": args.stale_days,
        "sections": sections,
    }


def build_html(payload):
    body = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return (PAGE_TEMPLATE
            .replace("__TITLE__", f"{payload['owner']} - repo status")
            .replace("__DATA__", body)
            .replace("__SCRIPT__", PAGE_SCRIPT))


def write_page(found, wanted, owner, root, states, results, args):
    payload = build_payload(found, wanted, owner, root, states, results, args)
    out = os.path.abspath(args.out or os.path.join(default_output_dir(), "repo-status.html"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(build_html(payload))
    return out


def default_output_dir():
    """Beside the script, so a run from any directory lands in the same place."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light;
    --plane:        #f9f9f7;
    --surface:      #fcfcfb;
    --ink:          #0b0b0b;
    --ink-2:        #52514e;
    --muted:        #898781;
    --grid:         #e1e0d9;
    --axis:         #c3c2b7;
    --hairline:     rgba(11,11,11,0.10);
    --empty:        #edece6;
    --risk:         #eb6834;
    --warn:         #eda100;
    --ok:           #1baf7a;
    --shadow:       0 1px 2px rgba(11,11,11,0.05);
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --plane:        #0d0d0d;
    --surface:      #1a1a19;
    --ink:          #ffffff;
    --ink-2:        #c3c2b7;
    --muted:        #898781;
    --grid:         #2c2c2a;
    --axis:         #383835;
    --hairline:     rgba(255,255,255,0.10);
    --empty:        #262624;
    --risk:         #d95926;
    --warn:         #c98500;
    --ok:           #199e70;
    --shadow:       none;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --plane:        #0d0d0d;
      --surface:      #1a1a19;
      --ink:          #ffffff;
      --ink-2:        #c3c2b7;
      --muted:        #898781;
      --grid:         #2c2c2a;
      --axis:         #383835;
      --hairline:     rgba(255,255,255,0.10);
      --empty:        #262624;
      --risk:         #d95926;
      --warn:         #c98500;
      --ok:           #199e70;
      --shadow:       none;
    }
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--plane);
    color: var(--ink);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
  .page { max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }

  header.top { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 22px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  .spacer { flex: 1 1 auto; }
  button.ghost {
    font: inherit; font-size: 12px; color: var(--ink-2); cursor: pointer;
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 6px; padding: 5px 10px;
  }
  button.ghost:hover { color: var(--ink); }
  :focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }

  .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0 18px; }
  .kpi {
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow);
  }
  .kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  .kpi .value { font-size: 26px; font-weight: 600; margin-top: 2px; letter-spacing: -0.02em; }
  .kpi .note { color: var(--ink-2); font-size: 12px; }
  .kpi.risk .value { color: var(--risk); }

  /* The run-order rail. The eight sections print in the order the work gets
     done, so the ordinal is information: it is the sequence, not decoration. */
  .rail {
    display: flex; flex-wrap: wrap; gap: 6px;
    padding: 10px 12px; margin-bottom: 12px;
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 10px; box-shadow: var(--shadow);
  }
  .rail a {
    display: flex; align-items: center; gap: 7px; text-decoration: none;
    padding: 5px 9px; border-radius: 7px; border: 1px solid transparent;
    color: var(--ink-2); font-size: 12.5px;
  }
  .rail a:hover { border-color: var(--hairline); background: var(--plane); color: var(--ink); }
  .rail a.off { color: var(--muted); }
  .rail .ord { font-size: 10.5px; color: var(--muted); letter-spacing: 0.04em; }
  .rail .badge {
    font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 20px;
    background: var(--empty); color: var(--ink-2);
    font-variant-numeric: tabular-nums;
  }
  .rail .badge.risk { background: var(--risk); color: #fff; }
  .rail .badge.warn { background: var(--warn); color: #1a1400; }
  .rail .badge.ok   { background: transparent; color: var(--ok); padding-left: 0; }

  .filters {
    display: flex; flex-wrap: wrap; align-items: flex-end; gap: 14px 18px;
    padding: 12px 14px; margin-bottom: 18px;
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 10px;
  }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field > span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  select, input[type="search"] {
    font: inherit; font-size: 13px; color: var(--ink); background: var(--surface);
    border: 1px solid var(--axis); border-radius: 6px; padding: 5px 8px; min-width: 200px;
  }

  .card {
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 10px; padding: 16px 18px 18px; margin-bottom: 18px; box-shadow: var(--shadow);
  }
  .card h2 { font-size: 15px; font-weight: 600; margin: 0; }
  .card .caption {
    display: block; color: var(--muted); font-size: 12px; margin: 2px 0 0; max-width: 70ch;
  }

  /* A <details> carries the disclosure semantics and keyboard handling that an
     ARIA-wired div would have to reimplement. */
  .sect { padding: 0; }
  .sect-head {
    display: grid; align-items: baseline; gap: 2px 12px; cursor: pointer;
    grid-template-columns: auto 1fr auto auto;
    grid-template-areas: "ord title tally caret" "ord caption caption caption";
    padding: 15px 18px 14px; list-style: none; border-radius: 10px;
  }
  .sect-head::-webkit-details-marker { display: none; }
  .sect-head:hover { background: var(--plane); }
  .sect-head:focus-visible { outline: 2px solid var(--ink); outline-offset: -2px; }
  .sect-head .ord {
    grid-area: ord; font-size: 11px; color: var(--muted); letter-spacing: 0.08em;
  }
  .sect-head h2 { grid-area: title; }
  .sect-head .caption { grid-area: caption; }
  .sect-head .tally {
    grid-area: tally; font-size: 12px; font-weight: 600;
    color: var(--muted); font-variant-numeric: tabular-nums;
  }
  .sect-head .tally.risk { color: var(--risk); }
  .sect-head .tally.warn { color: var(--warn); }
  .sect-head .tally.ok   { color: var(--ok); }
  .sect-head .caret {
    grid-area: caret; color: var(--muted); display: flex; align-items: center;
    transform: rotate(0deg); transition: transform 140ms ease;
  }
  .sect[open] > .sect-head > .caret { transform: rotate(90deg); }
  .sect-head:hover .caret { color: var(--ink-2); }
  .sect-body { padding: 0 18px 18px; }

  .grp { margin-top: 16px; }
  .grp-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
  .grp-label {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--ink-2);
  }
  .grp-note { font-size: 12px; color: var(--muted); }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--axis); flex: none; }
  .dot.risk { background: var(--risk); }
  .dot.warn { background: var(--warn); }
  .dot.ok   { background: var(--ok); }

  ul.items, ul.steps { list-style: none; margin: 0; padding: 0; }
  .it { padding: 9px 0 10px; border-top: 1px solid var(--grid); }
  .it:first-child { border-top: none; }
  /* Losable work earns a rule, not a wash of colour: the accent marks the row
     and the commands underneath stay the thing you read. */
  .it.risk { border-left: 2px solid var(--risk); padding-left: 11px; margin-left: -13px; }
  .it-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .it-name { font-size: 13.5px; font-weight: 600; color: var(--ink);
             text-decoration: none; overflow-wrap: anywhere; }
  a.it-name { border-bottom: 1px solid var(--axis); }
  a.it-name:hover { border-bottom-color: var(--ink); }
  .it-meta { font-size: 13px; color: var(--ink-2); }
  .it.risk .it-meta { color: var(--risk); }
  .it.ok .it-meta { color: var(--ok); }
  .tag {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--ink-2); background: var(--empty);
    border-radius: 4px; padding: 1px 5px;
  }

  .grp.dense ul.items { display: flex; flex-wrap: wrap; gap: 4px 6px; }
  .grp.dense .it {
    border: 1px solid var(--hairline); border-radius: 6px;
    padding: 3px 8px; background: var(--plane);
  }
  .grp.dense .it-name { font-size: 12px; font-weight: 500; color: var(--ink-2); }

  .steps { margin-top: 5px; }
  .st { font-size: 12.5px; color: var(--ink-2); padding: 2px 0; }
  .st a { display: block; color: inherit; text-decoration: none; }
  .st .cells { display: flex; align-items: baseline; gap: 2px 10px; flex-wrap: wrap; }
  /* Branch and repo names are long unbreakable tokens; a flex child will not
     shrink past its content without this. */
  .st .cells > span { min-width: 0; overflow-wrap: anywhere; }
  .st .key { color: var(--ink); }
  .st a .key { border-bottom: 1px solid var(--axis); }
  .st a:hover .key { border-bottom-color: var(--ink); }
  .st .when { color: var(--muted); font-variant-numeric: tabular-nums; flex: none; }
  .st .what { color: var(--ink-2); }
  .st .dim { color: var(--muted); }
  .st .warn { color: var(--warn); }
  .st.risk { color: var(--risk); }
  .st.warn { color: var(--warn); }
  .st.ok   { color: var(--ok); }
  .st.mute { color: var(--muted); }

  /* The command is the payload: the terminal report's whole promise is that
     every actionable finding carries the exact line that settles it. */
  .cmd {
    display: flex; align-items: center; gap: 10px; width: 100%;
    margin: 4px 0 2px; padding: 7px 10px; text-align: left;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px; line-height: 1.45; color: var(--ink);
    background: var(--empty); border: 1px solid transparent; border-radius: 7px;
    cursor: pointer; user-select: text;
  }
  .cmd:hover { border-color: var(--axis); }
  .cmd .sigil { color: var(--muted); flex: none; user-select: none; }
  .cmd .text { flex: 1 1 auto; min-width: 0; overflow-x: auto; white-space: pre; }
  .cmd .act {
    flex: none; font: inherit; font-size: 10.5px; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted); user-select: none;
  }
  .cmd:hover .act { color: var(--ink-2); }
  .cmd.done { border-color: var(--ok); }
  .cmd.done .act { color: var(--ok); }
  .cmd.failed { border-color: var(--risk); }
  .cmd.failed .act { color: var(--risk); }

  .clean { display: flex; align-items: center; gap: 8px; color: var(--ok); font-size: 13px; margin-top: 14px; }
  .skipped { color: var(--muted); font-size: 13px; margin-top: 14px; }
  .card.is-skipped { opacity: 0.55; }
  .none { color: var(--muted); font-size: 13px; padding: 18px 0; text-align: center; }

  .reveal { opacity: 0; transform: translateY(6px); animation: rise 380ms ease-out forwards; }
  @keyframes rise { to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) {
    .reveal { animation: none; opacity: 1; transform: none; }
    .cmd, .rail a, .sect-head .caret { transition: none; }
  }

  @media (max-width: 860px) {
    .kpis { grid-template-columns: repeat(2, 1fr); }
  }
  /* A long tally crowds the title out of its own row before the card overflows. */
  @media (max-width: 700px) {
    .sect-head {
      grid-template-columns: auto 1fr auto;
      grid-template-areas: "ord title caret" "ord caption caption" "ord tally tally";
    }
    .sect-head .tally { margin-top: 4px; }
  }
</style>
</head>
<body>
<div class="page">
  <header class="top">
    <h1 id="title"></h1>
    <span class="sub" id="generated"></span>
    <span class="spacer"></span>
    <button class="ghost" id="theme">Theme</button>
  </header>

  <section class="kpis" id="kpis"></section>

  <nav class="rail" id="rail" aria-label="Sections, in the order the work gets done"></nav>

  <section class="filters">
    <label class="field"><span>Show</span>
      <select id="show">
        <option value="findings">Findings only</option>
        <option value="all">Everything</option>
      </select>
    </label>
    <label class="field"><span>Filter</span>
      <input type="search" id="q" placeholder="repo, branch, path">
    </label>
    <span class="spacer"></span>
    <button class="ghost" id="fold"></button>
    <button class="ghost" id="copyall"></button>
  </section>

  <div id="sections"></div>
</div>

<script type="application/json" id="status-data">__DATA__</script>
<script>
__SCRIPT__
</script>
</body>
</html>
"""


PAGE_SCRIPT = r"""
const DATA = JSON.parse(document.getElementById('status-data').textContent);

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));
const tally = (section, n) => plural(n, section.unit[0], section.unit[1]);
const ordinal = i => String(i + 1).padStart(2, '0');

/* A section's weight is the worst tone anything inside it carries: work that
   only one disk holds outranks work that is merely piling up. */
const RANK = { risk: 3, warn: 2, ok: 1 };
function sectionTone(section) {
  let worst = null;
  for (const group of section.groups.filter(g => !g.info)) {
    for (const tone of [group.tone, ...group.items.flatMap(i => [i.tone, ...i.steps.map(s => s.tone)])]) {
      if (tone && RANK[tone] > (RANK[worst] || 0)) worst = tone;
    }
  }
  return worst;
}
/* Informational groups report context, not work, so they stay out of the count. */
const countItems = section =>
  section.groups.reduce((n, g) => n + (g.info ? 0 : g.items.length), 0);
const actionable = section => section.groups.some(g => !g.info);
function commandsIn(section, {advisory = true} = {}) {
  const out = [];
  for (const group of section.groups) {
    for (const it of group.items) {
      for (const st of it.steps) {
        if (st.command && (advisory || !st.advisory)) out.push(st.command);
      }
      if (it.command) out.push(it.command);
    }
  }
  return out;
}

/* Searchable text for an item, so filtering reaches the branch and path names
   that live in the steps rather than only the headline. */
function haystack(it) {
  return [it.name, it.meta, ...it.tags, it.command,
          ...it.steps.flatMap(s => [s.text, s.command,
                                    ...(s.cells || []).map(c => c.text)])]
    .filter(Boolean).join(' ').toLowerCase();
}

function commandBar(text) {
  const bar = el('button', 'cmd');
  bar.type = 'button';
  bar.append(el('span', 'sigil', '$'), el('span', 'text', text), el('span', 'act', 'copy'));
  bar.dataset.command = text;
  bar.addEventListener('click', () => copy(text, bar));
  return bar;
}

function copy(text, bar) {
  navigator.clipboard.writeText(text).then(() => {
    mark(bar, 'done', 'copied');
  }, err => {
    mark(bar, 'failed', 'select and copy');
    console.error('clipboard write failed', err);
  });
}

function mark(bar, cls, label) {
  const act = bar.querySelector('.act');
  bar.classList.remove('done', 'failed');
  bar.classList.add(cls);
  act.textContent = label;
  clearTimeout(bar._reset);
  bar._reset = setTimeout(() => {
    bar.classList.remove('done', 'failed');
    act.textContent = 'copy';
  }, 2200);
}

function stepBody(st) {
  if (!st.cells) return el('span', 'st-text', st.text);
  const row = el('span', 'cells');
  for (const c of st.cells) row.append(el('span', c.cls || 'what', c.text));
  return row;
}

function renderStep(st) {
  const li = el('li', 'st' + (st.tone ? ' ' + st.tone : ''));
  if (st.url) {
    const a = el('a');
    a.href = st.url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.append(stepBody(st));
    li.append(a);
  } else {
    li.append(stepBody(st));
  }
  if (st.command) {
    const wrap = el('div');
    wrap.append(commandBar(st.command));
    li.append(wrap);
  }
  return li;
}

function renderItem(it) {
  const li = el('li', 'it' + (it.tone ? ' ' + it.tone : ''));
  li.dataset.hay = haystack(it);
  const head = el('div', 'it-head');
  if (it.url) {
    const a = el('a', 'it-name mono', it.name);
    a.href = it.url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    head.append(a);
  } else {
    head.append(el('span', 'it-name mono', it.name));
  }
  if (it.meta) head.append(el('span', 'it-meta', it.meta));
  for (const tag of it.tags) head.append(el('span', 'tag', tag));
  li.append(head);
  if (it.steps.length) {
    const steps = el('ul', 'steps');
    for (const st of it.steps) steps.append(renderStep(st));
    li.append(steps);
  }
  if (it.command) li.append(commandBar(it.command));
  return li;
}

function renderGroup(group) {
  const box = el('div', 'grp' + (group.dense ? ' dense' : ''));
  const head = el('div', 'grp-head');
  head.append(el('span', 'dot' + (group.tone ? ' ' + group.tone : '')));
  head.append(el('span', 'grp-label', group.label));
  if (group.note) head.append(el('span', 'grp-note', group.note));
  box.append(head);
  const list = el('ul', 'items');
  for (const it of group.items) list.append(renderItem(it));
  box.append(list);
  return box;
}

function caret() {
  const span = el('span', 'caret');
  span.innerHTML = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">' +
    '<path d="M3 1.5 L7 5 L3 8.5" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  return span;
}

function renderSection(section, index) {
  const card = el('details', 'card sect reveal');
  card.id = 's-' + section.id;
  card.dataset.id = section.id;
  card.style.animationDelay = Math.min(index, 8) * 45 + 'ms';

  const head = el('summary', 'sect-head');
  head.append(el('span', 'ord mono', ordinal(index)),
              el('h2', null, section.title),
              el('span', 'caption', section.blurb));

  const tone = sectionTone(section);
  const count = countItems(section);
  const badge = el('span', 'tally' + (section.skipped ? '' : count ? ' ' + (tone || '') : ' ok'));
  badge.textContent = section.skipped ? 'skipped'
    : count ? tally(section, count) : 'clear';
  head.append(badge, caret());
  card.append(head);

  /* A section opens when it has work in it. A clear or skipped one stays folded:
     it is there to say it was checked, not to be read. */
  const open = !section.skipped && count > 0;
  card.open = open;
  card.dataset.defaultOpen = String(open);

  const body = el('div', 'sect-body');
  card.append(body);

  if (section.skipped) {
    card.classList.add('is-skipped');
    body.append(el('p', 'skipped', 'Not run — excluded by --only or --skip.'));
    return card;
  }
  if (!actionable(section)) {
    const line = el('p', 'clean');
    line.append(el('span', 'dot ok'), el('span', null, section.clean));
    body.append(line);
    for (const group of section.groups) body.append(renderGroup(group));
    return card;
  }
  for (const group of section.groups) body.append(renderGroup(group));
  body.append(el('p', 'none', 'Nothing matches this filter.'));
  return card;
}

function renderKpis() {
  const live = DATA.sections.filter(s => !s.skipped);
  /* Only the uncommitted section measures work a dead disk would take: a
     held-back branch is already one of its rows, so counting the tidy-up
     section too would count the same commits twice. */
  const atRisk = live.filter(s => s.id === 'uncommitted').reduce((n, s) => n + countItems(s), 0);
  const commands = everyCommand().length;
  const tiles = [
    ['Projects', DATA.repos, 'owned on GitHub', false],
    ['Clones', DATA.clones, DATA.root.replace(/^\/Users\/[^/]+/, '~'), false],
    ['At risk', atRisk, 'held by one clone and nothing else', true],
    ['Commands', commands, 'ready to paste', false],
  ];
  const box = document.getElementById('kpis');
  tiles.forEach(([label, value, note, warnOnValue]) => {
    const tile = el('div', 'kpi' + (warnOnValue && value ? ' risk' : ''));
    tile.append(el('div', 'label', label), el('div', 'value', String(value)),
                el('div', 'note', note));
    box.append(tile);
  });
}

function renderRail() {
  const rail = document.getElementById('rail');
  DATA.sections.forEach((section, i) => {
    const a = el('a', section.skipped ? 'off' : null);
    a.href = '#s-' + section.id;
    a.append(el('span', 'ord mono', ordinal(i)), el('span', 'nm', section.title));
    const count = countItems(section);
    const tone = sectionTone(section);
    const badge = el('span', 'badge' + (section.skipped ? '' : count ? ' ' + (tone || '') : ' ok'));
    badge.textContent = section.skipped ? 'skipped' : count ? String(count) : 'clear';
    a.title = section.skipped ? 'Not run' : count ? tally(section, count) : section.clean;
    a.append(badge);
    rail.append(a);
  });
}

function applyFilter() {
  const query = document.getElementById('q').value.trim().toLowerCase();
  const findingsOnly = document.getElementById('show').value === 'findings';
  for (const card of document.querySelectorAll('.sect')) {
    const section = DATA.sections.find(s => s.id === card.dataset.id);
    let shown = 0;
    for (const li of card.querySelectorAll('.it')) {
      const hit = !query || li.dataset.hay.includes(query);
      li.hidden = !hit;
      if (hit) shown++;
    }
    for (const grp of card.querySelectorAll('.grp')) {
      grp.hidden = ![...grp.querySelectorAll('.it')].some(li => !li.hidden);
    }
    const empty = card.querySelector('.none');
    if (empty) empty.hidden = shown > 0;
    const quiet = section.skipped || !section.groups.some(g => !g.info);
    card.hidden = findingsOnly ? (quiet || shown === 0) : (query !== '' && shown === 0 && !quiet);
    /* A match inside a folded section would otherwise be invisible; clearing the
       query hands every section back to the state it started in. */
    if (query) {
      if (shown > 0) card.open = true;
    } else {
      card.open = card.dataset.defaultOpen === 'true';
    }
  }
  syncFold();
}

function openSections() {
  return [...document.querySelectorAll('.sect')].filter(c => !c.hidden && c.open);
}

function syncFold() {
  const cards = [...document.querySelectorAll('.sect')].filter(c => !c.hidden);
  const fold = document.getElementById('fold');
  fold.disabled = cards.length === 0;
  fold.textContent = openSections().length ? 'Collapse all' : 'Expand all';
}

function everyCommand() {
  return DATA.sections.filter(s => !s.skipped)
    .flatMap(s => commandsIn(s, {advisory: false}));
}

function init() {
  const owner = DATA.owner;
  document.getElementById('title').textContent = owner + ' — repo status';
  document.getElementById('generated').textContent =
    'read ' + new Date(DATA.generated).toLocaleString() +
    ' · stale after ' + plural(DATA.stale_days, 'day', 'days');
  renderKpis();
  renderRail();
  const host = document.getElementById('sections');
  DATA.sections.forEach((section, i) => host.append(renderSection(section, i)));

  const all = everyCommand();
  const copyall = document.getElementById('copyall');
  copyall.textContent = 'Copy ' + plural(all.length, 'command', 'commands');
  copyall.disabled = all.length === 0;
  copyall.addEventListener('click', () => {
    navigator.clipboard.writeText(all.join('\n')).then(() => {
      copyall.textContent = 'Copied ' + plural(all.length, 'command', 'commands');
      setTimeout(() => {
        copyall.textContent = 'Copy ' + plural(all.length, 'command', 'commands');
      }, 2200);
    }, err => {
      copyall.textContent = 'Clipboard refused';
      console.error('clipboard write failed', err);
    });
  });

  document.getElementById('q').addEventListener('input', applyFilter);
  document.getElementById('show').addEventListener('change', applyFilter);

  document.getElementById('fold').addEventListener('click', () => {
    const expand = openSections().length === 0;
    for (const card of document.querySelectorAll('.sect')) {
      if (!card.hidden) card.open = expand;
    }
    syncFold();
  });
  host.addEventListener('toggle', syncFold, true);

  /* Jumping to a section from the rail has to open it, or the anchor lands on a
     folded header that shows nothing. */
  document.getElementById('rail').addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link) return;
    const card = document.getElementById(link.hash.slice(1));
    if (card) {
      card.open = true;
      syncFold();
    }
  });

  applyFilter();

  document.getElementById('theme').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const now = root.dataset.theme || (dark ? 'dark' : 'light');
    root.dataset.theme = now === 'dark' ? 'light' : 'dark';
  });
}

init();
"""


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner", help="GitHub owner to probe (default: authenticated user)")
    parser.add_argument("--root", help="local clone tree (default: ~/src/github/<owner>)")
    parser.add_argument("--depth", type=int, default=3,
                        help="directory levels to search for clones (default: 3)")
    parser.add_argument("--repo", action="append", dest="repos", metavar="NAME",
                        help="probe only this repo (repeatable); NAME or owner/NAME")
    parser.add_argument("--only", action="append", choices=SECTIONS, metavar="SECTION",
                        help="run only this section (repeatable): " + ", ".join(SECTIONS))
    parser.add_argument("--skip", action="append", choices=SECTIONS, metavar="SECTION",
                        help="skip this section (repeatable)")
    parser.add_argument("--fix", action="store_true",
                        help="carry out the reconciliation, asking before each move or deletion")
    parser.add_argument("--yes", action="store_true",
                        help="with --fix, move and delete without asking")
    parser.add_argument("--stale-days", type=int, default=1,
                        help="a remote branch with no open PR is orphaned after this "
                             "many days without a commit (default: 1)")
    parser.add_argument("--include-forks", action="store_true",
                        help="scan forks, overriding `forks: true` in {}".format(IGNORE_FILE))
    parser.add_argument("--no-ignore", action="store_true",
                        help="scan every repo, disregarding {}".format(IGNORE_FILE))
    parser.add_argument("--include-archived", action="store_true",
                        help="scan archived repos, overriding `archived: true` in "
                             "{}".format(IGNORE_FILE))
    parser.add_argument("--include-release-commits", action="store_true",
                        help="count the current release's own version-bump commit as pending")
    parser.add_argument("--max-commits", type=int, default=10,
                        help="commit subjects to show per repo (default: 10)")
    parser.add_argument("--repo-limit", type=int, default=300,
                        help="max repos to enumerate (default: 300)")
    parser.add_argument("--jobs", type=int, default=8, help="parallel probes (default: 8)")
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of a report")
    parser.add_argument("--out", help="where the page is written "
                                      "(default: output/repo-status.html beside this script)")
    parser.add_argument("--open", action="store_true", help="open the page when the run ends")
    parser.add_argument("--all-details", action="store_true",
                        help="show clean repos and clones too")
    args = parser.parse_args()

    if not shutil.which("gh"):
        sys.exit("gh CLI not found — install it and run `gh auth login`")

    wanted = set(args.only or SECTIONS) - set(args.skip or [])
    if not wanted:
        sys.exit("--only and --skip leave no sections to run")

    try:
        owner = args.owner or current_login()
    except GhError as err:
        sys.exit(f"gh auth failed: {err}\nRun `gh auth login` (or `gh auth refresh`) and retry.")

    root = os.path.abspath(os.path.expanduser(
        args.root or os.path.join("~", "src", "github", owner)))

    try:
        ignore = load_ignore(enabled=not args.no_ignore)
    except IgnoreError as err:
        sys.exit(str(err))
    # An explicit flag outranks the file.
    if args.include_archived:
        ignore.archived = False
    if args.include_forks:
        ignore.forks = False

    held = []
    if args.repos:
        # Naming a repo outright asks for it, so the ignore list stays out of it.
        repos = [repo_record(name if "/" in name else f"{owner}/{name}") for name in args.repos]
    else:
        listed = [(r, hold_back(r, ignore))
                  for r in list_repos(owner, args.repo_limit)]
        held = [(r, why) for r, why in listed if why]
        repos = [r for r, why in listed if not why]
    repos.sort(key=lambda r: r["name"].lower())

    if not repos:
        sys.exit(f"no repositories found for {owner}")

    if held and not args.json:
        print(f"scope: {len(repos)} of the {len(repos) + len(held)} repo(s) you own; "
              f"{len(held)} held back")
        by_reason = {}
        for repo, why in held:
            by_reason.setdefault(why, []).append(repo)
        for why in sorted(by_reason, key=lambda w: (-len(by_reason[w]), w)):
            named = sorted(by_reason[why], key=lambda r: r["name"].lower())
            print(f"  {why} ({len(named)}): "
                  + ", ".join(name_link(r["name"], r["url"]) for r in named))
        print("  widen with " + ", ".join(sorted(HOLD_WIDENS[w] for w in by_reason)))

    local_sections = wanted & {"reconcile", "uncommitted", "local-branches",
                               "orphan-branches", "behind"}
    clones = []
    if local_sections:
        if not os.path.isdir(root):
            sys.exit(f"clone tree not found: {root} (pass --root)")
        clones = find_clones(root, args.depth)
        # Drop an ignored repo's clone here, before reconcile spends an API call
        # classifying it as a clone with no matching repo.
        clones = [c for c in clones if not ignore.skips(c["name"])]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda r: probe_repo(r, wanted, args.max_commits), repos))

    if args.include_release_commits:
        for r in results:
            r["ahead"] = r["ahead_raw"]
            r["oldest"] = r["oldest_raw"]
            for c in r["commits"]:
                c["release_commit"] = False

    default_by_repo = {r["repo"].lower(): r["branch"] for r in results}

    quiet = args.json
    found = {}

    if "reconcile" in wanted:
        found["reconcile"] = collect_reconcile(repos, held, clones, root, owner, args.jobs)
        if not quiet:
            clones = section_reconcile(found["reconcile"], clones, args.fix, args.yes)

    states = []
    if wanted & {"uncommitted", "local-branches", "orphan-branches", "behind"}:
        def probe(clone):
            key = f"{clone['owner']}/{clone['name']}".lower()
            return probe_clone(clone, default_by_repo.get(key), fetch=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            states = list(pool.map(probe, clones))

    clone_by_repo = {f"{c['owner']}/{c['name']}".lower(): c
                     for c in clones if c["owner"] and c["name"]}

    if "uncommitted" in wanted:
        found["uncommitted"] = collect_uncommitted(states, args.all_details)
        if not quiet:
            section_uncommitted(found["uncommitted"], states)
    if "local-branches" in wanted:
        found["local-branches"] = collect_local_branches(states)
        if not quiet:
            section_local_branches(found["local-branches"], args.fix, args.yes)
    if "orphan-branches" in wanted:
        found["orphan-branches"] = collect_orphan_branches(
            results, clone_by_repo, args.stale_days)
        if not quiet:
            section_orphan_branches(found["orphan-branches"], args.stale_days,
                                    args.fix, args.yes)
    if "prs" in wanted:
        found["prs"] = collect_prs(results)
        if not quiet:
            section_prs(*found["prs"])
    if "unreleased" in wanted:
        found["unreleased"] = collect_unreleased(results, args.all_details)
        if not quiet:
            section_unreleased(found["unreleased"], args.max_commits)
    if "issues" in wanted:
        found["issues"] = collect_issues(results)
        if not quiet:
            section_issues(*found["issues"])
    if "behind" in wanted:
        found["behind"] = collect_behind(states, args.all_details)
        if not quiet:
            section_behind(found["behind"], states, args.fix)

    if args.json:
        json.dump({"repos": results, "clones": states}, sys.stdout, indent=2)
        print()
        return

    out = write_page(found, wanted, owner, root, states, results, args)
    print(f"\n-> {out}", file=sys.stderr)
    if args.open:
        webbrowser.open(f"file://{out}")


if __name__ == "__main__":
    main()
