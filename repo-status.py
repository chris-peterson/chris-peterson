#!/usr/bin/env python3
"""Report on the GitHub projects you own, and reconcile them with your local clones.

Sections, in the order they print:

  reconcile        the clone tree under --root vs the repos you own on GitHub,
                   each clone expected at <root>/<repo name>
  freshness        each clone's fetch, ahead/behind and working-tree state
  local-branches   local branches already merged into the default branch
  orphan-branches  remote branches with no open PR that have gone stale
  unreleased       commits on the default branch since the last release or tag
  prs              open pull requests
  issues           open issues, grouped by milestone

Remote state comes from the GitHub API through `gh`, so it reflects the server
rather than whatever the clones happen to hold. Local state comes from the
clones themselves, each one fetched with --prune first.

Without --fix the run only reads (and fetches). With --fix it clones what is
missing and fast-forwards what is cleanly behind, then asks y/n before each
change that moves or removes something: relocating a clone onto its own path,
deleting a clone whose repo is gone, deleting a merged local branch, deleting an
orphaned remote branch.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

SECTIONS = ["reconcile", "freshness", "local-branches", "orphan-branches",
            "unreleased", "prs", "issues"]


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


def list_repos(owner, include_forks, include_archived, limit):
    args = ["repo", "list", owner, "--limit", str(limit), "--json",
            "name,owner,defaultBranchRef,isPrivate,isFork,isArchived,pushedAt,url,sshUrl"]
    if not include_forks:
        args.append("--source")
    if not include_archived:
        args.append("--no-archived")
    return json.loads(gh(args))


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
        "error": None,
    }
    try:
        if "orphan-branches" in wanted:
            result["branches"], result["branches_truncated"] = remote_branches(
                result["owner"], result["name"])
        if wanted & {"prs", "orphan-branches"}:
            result["pulls"] = open_pulls(full_name)
        if "issues" in wanted:
            result["issues"] = open_issues(full_name)
        # An empty repo has no baseline and no commits; it drops out of the
        # unreleased section with the rest of the never-released projects.
        if "unreleased" in wanted and branch:
            marker = latest_release(full_name) or latest_tag(full_name)
            result["release"] = marker
            if marker:
                diff = unreleased(full_name, marker["tag"], branch)
                result.update(diff)
                result["commits"] = list(reversed(diff["commits"]))[:max_commits]
                result["truncated"] = diff["truncated"] or len(diff["commits"]) > max_commits
    except GhError as err:
        result["error"] = str(err)
    return result


# --------------------------------------------------------------------------- #
# local probes
# --------------------------------------------------------------------------- #

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
    info["dirty"] = len((git(path, "status", "--porcelain", check=False) or "").splitlines())

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

    tracking = {}
    for line in (git(path, "for-each-ref", "--format=%(refname:short)\t%(upstream:track)",
                     "refs/heads/", check=False) or "").splitlines():
        name, _, track = line.partition("\t")
        tracking[name] = track
        if "[gone]" in track and name not in (default, info["branch"]):
            info["gone_branches"].append(name)

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
    if not TTY:
        return f"{label}  {url}"
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


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

def section_reconcile(repos, clones, root, owner, fix, assume_yes, jobs):
    """Clone tree vs owned repos. Returns the clones that survive the pass."""
    heading("RECONCILE", f"{root} vs the {len(repos)} repo(s) you own on GitHub")

    owned = {r["name"].lower(): r for r in repos}
    mine = [c for c in clones if c["owner"] and c["owner"].lower() == owner.lower()]
    foreign = [c for c in clones if c not in mine]
    cloned = {c["name"].lower() for c in mine if c["name"]}

    missing = [r for r in repos if r["name"].lower() not in cloned]
    unmatched = [c for c in mine if c["name"] and c["name"].lower() not in owned]

    # A clone whose origin is absent from the owned list is either a repo that
    # was deleted or renamed, or one this run filtered out (archived, fork).
    def classify(clone):
        full = f"{clone['owner']}/{clone['name']}"
        try:
            info = gh_json(f"repos/{full}")
        except GhError as err:
            if "Not Found" in str(err) or "404" in str(err):
                return clone, "gone", None
            return clone, "error", str(err)
        moved = info["full_name"].lower() != full.lower()
        return clone, "moved" if moved else "filtered", info

    verdicts = []
    if unmatched:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            verdicts = list(pool.map(classify, unmatched))

    gone = [(c, i) for c, kind, i in verdicts if kind == "gone"]
    moved = [(c, i) for c, kind, i in verdicts if kind == "moved"]
    filtered = [(c, i) for c, kind, i in verdicts if kind == "filtered"]
    failed = [(c, i) for c, kind, i in verdicts if kind == "error"]

    # Every clone belongs at <root>/<repo name>. A clone nested under a grouping
    # directory, or filed under an upstream's name when the fork is your own,
    # is the finding — the layout is what makes the two agree at a glance.
    canonical_names = {r["name"].lower(): r["name"] for r in repos}
    canonical_names.update({i["name"].lower(): i["name"] for _, i in filtered})
    misplaced = []
    for clone in mine:
        canonical = canonical_names.get((clone["name"] or "").lower())
        if not canonical:
            continue
        leaf = os.path.basename(clone["rel"])
        if os.path.dirname(clone["rel"]) or leaf.lower() != canonical.lower():
            misplaced.append((clone, canonical))

    clean = True

    if missing:
        clean = False
        print()
        print(paint(f"  on GitHub, not cloned ({len(missing)})", "1"))
        for repo in sorted(missing, key=lambda r: r["name"].lower()):
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
        for clone, _ in sorted(gone, key=lambda p: p[0]["rel"].lower()):
            dirty = len((git(clone["path"], "status", "--porcelain", check=False) or "").splitlines())
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
        for clone, canonical in sorted(misplaced, key=lambda pair: pair[0]["rel"].lower()):
            target = os.path.join(root, canonical)
            print(f"    {clone['rel']}  ->  {canonical}")
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
        for clone, info in sorted(moved, key=lambda p: p[0]["rel"].lower()):
            print(f"    {clone['rel']}  ->  renamed to {info['full_name']}")
            print(f"      git -C {clone['path']} remote set-url origin {info['ssh_url']}")

    if filtered:
        print()
        print(f"  cloned, filtered out of this run ({len(filtered)}): "
              + ", ".join(sorted(c["rel"] for c, _ in filtered)))
        print("    (archived or fork — pass --include-archived / --include-forks to include)")

    if failed:
        clean = False
        print()
        print(paint(f"  could not be checked ({len(failed)})", "1"))
        for clone, err in failed:
            print(f"    {clone['rel']}: {err}")

    if foreign:
        print()
        print(f"  clones of other owners ({len(foreign)}), reported for freshness only:")
        for clone in sorted(foreign, key=lambda c: c["rel"].lower()):
            print(f"    {clone['rel']}  ->  {clone['origin'] or 'no origin remote'}")

    if clean:
        print()
        print("  in sync")

    return clones


def section_freshness(states, fix, all_details):
    heading("CLONE FRESHNESS", "ahead/behind is the checked-out branch against its upstream")
    rows = []
    for s in sorted(states, key=lambda s: s["rel"].lower()):
        notes = []
        if s["fetch_error"]:
            notes.append(paint(f"fetch failed: {s['fetch_error']}", "31"))
        if s["detached"]:
            notes.append("detached HEAD")
        elif not s["upstream"]:
            notes.append("no upstream")
        if s["dirty"]:
            notes.append(f"{s['dirty']} uncommitted")
        if s["ahead"]:
            notes.append(f"{s['ahead']} unpushed")
        if s["behind"]:
            notes.append(f"{s['behind']} behind")
        if s["default_behind"]:
            notes.append(f"{s['default']} is {s['default_behind']} behind origin")
        if s["default_ahead"]:
            notes.append(f"{s['default']} has {s['default_ahead']} unpushed")
        if not notes and not all_details:
            continue
        rows.append((s, notes or ["up to date"]))

    if not rows:
        print()
        print(f"  all {len(states)} clone(s) clean and up to date")
        return

    width = max(len(s["rel"]) for s, _ in rows)
    print()
    for s, notes in rows:
        branch = s["branch"] or "(detached)"
        print(f"  {s['rel']:<{width}}  {branch:<24}  " + "; ".join(notes))
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


def section_local_branches(states, fix, assume_yes):
    heading("LOCAL BRANCHES TO CLEAN UP",
            "fully merged into the default branch, or tracking a deleted remote")
    found = False
    for s in sorted(states, key=lambda s: s["rel"].lower()):
        merged, gone = s["merged_branches"], s["gone_branches"]
        if not merged and not gone:
            continue
        found = True
        print()
        print(paint(f"  {s['rel']}  ({len(merged) + len(gone)})", "1"))
        if merged:
            print(f"    merged into {s['default']} ({len(merged)}): " + ", ".join(sorted(merged)))
        if gone:
            print(f"    upstream deleted ({len(gone)}): " + ", ".join(sorted(gone)))
        branches = sorted(merged) + sorted(gone)
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
    if not found:
        print()
        print("  nothing to clean up")


def section_orphan_branches(results, clone_by_repo, stale_days, fix, assume_yes):
    heading("ORPHANED REMOTE BRANCHES",
            f"no open PR and no commit in {stale_days} day(s)")
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    found = False
    for r in sorted(results, key=lambda r: r["repo"].lower()):
        if r["error"]:
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
        found = True
        clone = clone_by_repo.get(r["repo"].lower())
        print()
        print(paint(f"  {r['repo']}", "1"))
        if r["branches_truncated"]:
            print("    note: more than 100 branches; only the first 100 were checked")
        for branch in sorted(orphans, key=lambda b: b["date"] or ""):
            merged = ""
            if clone:
                merged = "  [merged into " + r["branch"] + "]" if branch_merged_remotely(
                    clone["path"], r["branch"], branch["name"]) else "  [not merged]"
            days = age_days(branch["date"])
            aged = f"{days}d" if days is not None else "unknown age"
            label = f"    {branch['name']}  {branch['sha']}  {fmt_date(branch['date'])} ({aged}){merged}"
            print(link(label, f"{r['url']}/tree/{branch['name']}"))
        names = [b["name"] for b in sorted(orphans, key=lambda b: b["date"] or "")]
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
    if not found:
        print()
        print("  none")


def section_unreleased(results, max_commits, all_details):
    heading("UNRELEASED CHANGES",
            "longest-waiting project first; commits newest first\n"
            "counts exclude the version-bump commit of the current release "
            "(--include-release-commits to count it)")
    released = [r for r in results if r["release"] or r["error"]]
    ordered = sorted(released, key=lambda r: (r["error"] is not None, r["oldest"] or "~"))
    detailed = [r for r in ordered if all_details or r["ahead"] or r["error"]]
    if not detailed:
        print()
        print("  every released project is up to date")
        return
    for r in detailed:
        rel = r["release"]
        print()
        if r["error"]:
            print(paint(f"  {r['repo']}: {r['error']}", "1"))
            continue
        qualifier = ", tag only" if rel["kind"] == "tag" else (
            ", prerelease" if rel["prerelease"] else "")
        span = rel["tag"] + "..." + r["branch"]
        head = (f"  {r['repo']}: {r['ahead']} commit(s) since {rel['tag']}"
                f" ({fmt_date(rel['published'])}{qualifier})")
        print(paint(link(head, f"{r['url']}/compare/{span}"), "1"))
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


def section_prs(results):
    heading("OPEN PULL REQUESTS", "oldest first")
    rows = [(r, p) for r in results for p in r["pulls"]]
    if not rows:
        print()
        print("  none")
        return
    rows.sort(key=lambda pair: pair[1]["created"] or "")
    print()
    for r, p in rows:
        days = age_days(p["created"])
        flags = " [draft]" if p["draft"] else ""
        label = (f"  {r['repo']}#{p['number']}{flags}  {p['title']}")
        print(link(label, p["url"]))
        print(f"      {p['head']} -> {p['base']}  by {p['author']}  "
              f"opened {fmt_date(p['created'])} ({days}d)")


def section_issues(results):
    heading("OPEN ISSUES", "grouped by milestone, soonest due date first")
    rows = [(r, i) for r in results for i in r["issues"]]
    if not rows:
        print()
        print("  none")
        return

    groups = {}
    for r, issue in rows:
        key = issue["milestone"] or "(no milestone)"
        groups.setdefault(key, {"due": issue["milestone_due"], "items": []})
        groups[key]["items"].append((r, issue))

    def order(item):
        title, group = item
        if title == "(no milestone)":
            return (2, "", title)
        return (0 if group["due"] else 1, group["due"] or "", title)

    for title, group in sorted(groups.items(), key=order):
        due = f"  (due {fmt_date(group['due'])})" if group["due"] else ""
        print()
        print(paint(f"  {title}{due}  — {len(group['items'])} issue(s)", "1"))
        for r, issue in sorted(group["items"], key=lambda pair: pair[1]["created"] or ""):
            labels = f"  [{', '.join(issue['labels'])}]" if issue["labels"] else ""
            label = f"    {r['repo']}#{issue['number']}  {issue['title']}{labels}"
            print(link(label, issue["url"]))


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
    parser.add_argument("--include-forks", action="store_true")
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--include-release-commits", action="store_true",
                        help="count the current release's own version-bump commit as pending")
    parser.add_argument("--max-commits", type=int, default=10,
                        help="commit subjects to show per repo (default: 10)")
    parser.add_argument("--repo-limit", type=int, default=300,
                        help="max repos to enumerate (default: 300)")
    parser.add_argument("--jobs", type=int, default=8, help="parallel probes (default: 8)")
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of a report")
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

    if args.repos:
        repos = [repo_record(name if "/" in name else f"{owner}/{name}") for name in args.repos]
    else:
        repos = list_repos(owner, args.include_forks, args.include_archived, args.repo_limit)
    repos.sort(key=lambda r: r["name"].lower())

    if not repos:
        sys.exit(f"no repositories found for {owner}")

    local_sections = wanted & {"reconcile", "freshness", "local-branches", "orphan-branches"}
    clones = []
    if local_sections:
        if not os.path.isdir(root):
            sys.exit(f"clone tree not found: {root} (pass --root)")
        clones = find_clones(root, args.depth)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda r: probe_repo(r, wanted, args.max_commits), repos))

    if args.include_release_commits:
        for r in results:
            r["ahead"] = r["ahead_raw"]
            r["oldest"] = r["oldest_raw"]
            for c in r["commits"]:
                c["release_commit"] = False

    default_by_repo = {r["repo"].lower(): r["branch"] for r in results}

    if "reconcile" in wanted:
        clones = section_reconcile(repos, clones, root, owner, args.fix, args.yes, args.jobs)

    states = []
    if wanted & {"freshness", "local-branches", "orphan-branches"}:
        def probe(clone):
            key = f"{clone['owner']}/{clone['name']}".lower()
            return probe_clone(clone, default_by_repo.get(key), fetch=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            states = list(pool.map(probe, clones))

    clone_by_repo = {f"{c['owner']}/{c['name']}".lower(): c
                     for c in clones if c["owner"] and c["name"]}

    if args.json:
        json.dump({"repos": results, "clones": states}, sys.stdout, indent=2)
        print()
        return

    if "freshness" in wanted:
        section_freshness(states, args.fix, args.all_details)
    if "local-branches" in wanted:
        section_local_branches(states, args.fix, args.yes)
    if "orphan-branches" in wanted:
        section_orphan_branches(results, clone_by_repo, args.stale_days, args.fix, args.yes)
    if "unreleased" in wanted:
        section_unreleased(results, args.max_commits, args.all_details)
    if "prs" in wanted:
        section_prs(results)
    if "issues" in wanted:
        section_issues(results)


if __name__ == "__main__":
    main()
