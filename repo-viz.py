#!/usr/bin/env python3
"""Chart where your attention went across your GitHub projects, week by week.

Reads the GitHub GraphQL API through `gh` and writes a self-contained HTML file —
no network at view time, no CDN, no build step.

  ./repo-viz.py                      # write repo-viz.html and open it
  ./repo-viz.py --months 24          # widen the window
  ./repo-viz.py --owner some-user    # someone else's public projects
  ./repo-viz.py --out /tmp/x.html --no-open
  ./repo-viz.py --json               # the gathered data, for another tool

Only commits the owner authored count. A fork tracking upstream lands hundreds
of commits on its default branch that were never anyone's attention, and they
would drown out the work that was.

The page carries five views of the same slice, all scoped by one filter row:

  focus      weekly commits stacked by project — how much, and where it went
  map        every project against every week — what was alive when
  rhythm     the hours of the week those commits land in, in your own timezone
  treemap    what the work amounts to: size, language, and recent attention
  table      every number the charts show, sortable, nothing hover-gated
"""

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta, timezone

import yaml

PAGE = 50
WEEK_MINUTES = 7 * 24 * 60


IGNORE_FILE = "ignore.yml"
IGNORE_KEYS = {"archived", "forks", "repos"}
REPO_KEYS = {"name", "reason"}


class IgnoreError(RuntimeError):
    pass


class Ignore:
    def __init__(self, archived=False, forks=False, names=()):
        self.archived = archived
        self.forks = forks
        # {lowercased name: the reason it is skipped, or None when it gives none}
        self.names = dict(names)

    def skips(self, name, archived=False, fork=False):
        return self.reason(name, archived, fork) is not None

    def reason(self, name, archived=False, fork=False):
        key = (name or "").lower()
        if key in self.names:
            return self.names[key] or IGNORE_FILE
        if self.archived and archived:
            return "archived"
        return "fork" if self.forks and fork else None

    def __bool__(self):
        return bool(self.archived or self.forks or self.names)

    def describe(self):
        parts = [state for state, on in (("archived", self.archived),
                                         ("forks", self.forks)) if on]
        if self.names:
            parts.append(", ".join(f"{n} ({self.names[n]})" if self.names[n] else n
                                   for n in sorted(self.names)))
        return "; ".join(parts) or "nothing"


def repo_names(entries):
    """Read `repos:` into {name: reason}, where an entry gives its reason or not.

    A bare `- name` skips the repo without saying why and reports under the file
    itself; a `- name:` / `reason:` mapping reports under the reason instead, so
    the scope line groups the ones skipped for the same cause together."""
    named = {}
    for entry in entries:
        if isinstance(entry, str):
            if not entry.strip():
                raise IgnoreError(f"{IGNORE_FILE}: `repos` holds a blank entry")
            named[entry.strip().lower()] = None
            continue
        if not isinstance(entry, dict):
            raise IgnoreError(f"{IGNORE_FILE}: `repos` entry {entry!r} is neither a "
                              "name nor a `name:` / `reason:` mapping")
        unknown = set(entry) - REPO_KEYS
        if unknown:
            raise IgnoreError(f"{IGNORE_FILE}: `repos` entry {entry!r} has unknown key "
                              f"{', '.join(sorted(unknown))!r}, expected one of "
                              f"{', '.join(sorted(REPO_KEYS))}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise IgnoreError(f"{IGNORE_FILE}: `repos` entry {entry!r} needs a `name`")
        reason = entry.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise IgnoreError(f"{IGNORE_FILE}: `reason` for `{name}` takes a phrase, "
                              "which is what the scope line groups it under")
        named[name.strip().lower()] = reason.strip() if reason else None
    return named


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
    return Ignore(archived=states["archived"], forks=states["forks"],
                  names=repo_names(repos))


class GhError(RuntimeError):
    pass


REPO_QUERY = """
query($owner: String!, $since: GitTimestamp!, $author: ID!, $cursor: String) {
  repositoryOwner(login: $owner) {
    repositories(first: %d, after: $cursor, ownerAffiliations: OWNER,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        nameWithOwner
        url
        description
        isFork
        isArchived
        isPrivate
        diskUsage
        stargazerCount
        createdAt
        pushedAt
        primaryLanguage { name }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          totalSize
          edges { size node { name } }
        }
        defaultBranchRef {
          name
          target {
            ... on Commit {
              everyone: history(since: $since) { totalCount }
              history(since: $since, author: {id: $author}, first: 100) {
                totalCount
                pageInfo { hasNextPage endCursor }
                nodes { committedDate }
              }
            }
          }
        }
      }
    }
  }
}
""" % PAGE

HISTORY_QUERY = """
query($owner: String!, $name: String!, $since: GitTimestamp!, $author: ID!,
      $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(since: $since, author: {id: $author}, first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { committedDate }
          }
        }
      }
    }
  }
}
"""


def gh(args):
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        args += ["-F", f"{key}={value}"]
    payload = json.loads(gh(args))
    if "errors" in payload:
        raise GhError("; ".join(e.get("message", "?") for e in payload["errors"]))
    return payload["data"]


def window(now, months):
    """The trailing whole weeks, starting on a Monday at midnight UTC."""
    weeks = max(4, round(months * 52 / 12))
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday - timedelta(weeks=weeks - 1), weeks


def commit_dates(owner, name, author, history, since):
    """Every committedDate in the window, paginating past the first page."""
    dates = [n["committedDate"] for n in history["nodes"]]
    page = history["pageInfo"]
    while page["hasNextPage"]:
        data = graphql(HISTORY_QUERY, owner=owner, name=name, since=since,
                       author=author, cursor=page["endCursor"])
        more = data["repository"]["defaultBranchRef"]["target"]["history"]
        dates += [n["committedDate"] for n in more["nodes"]]
        page = more["pageInfo"]
    return dates


def gather(owner, author, start, weeks, progress=True, ignore=None):
    since = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    ignore = ignore or Ignore()
    repos, cursor, skipped = [], None, []

    while True:
        data = graphql(REPO_QUERY, owner=owner, since=since, author=author,
                       cursor=cursor)
        holder = data.get("repositoryOwner")
        if not holder:
            raise GhError(f"no such GitHub owner: {owner}")
        page = holder["repositories"]
        for node in page["nodes"]:
            # Skipping before shape() is the point: shape() paginates the repo's
            # whole commit history when one page won't hold it.
            if ignore.skips(node["name"], node["isArchived"], node["isFork"]):
                skipped.append(node["name"])
                continue
            repos.append(shape(owner, author, node, since, start, progress))
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    if skipped and progress:
        print(f"  {IGNORE_FILE}: skipped {len(skipped)} "
              f"({', '.join(sorted(skipped))})", file=sys.stderr)

    repos.sort(key=lambda r: r["commits"], reverse=True)
    return {
        "owner": owner,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "windowStart": since,
        "weeks": weeks,
        "repos": repos,
    }


def shape(owner, author, node, since, start, progress):
    """One API node, flattened into what the page plots."""
    branch = node.get("defaultBranchRef") or {}
    target = branch.get("target") or {}
    history = target.get("history") or {"totalCount": 0, "nodes": [],
                                        "pageInfo": {"hasNextPage": False}}
    if history["pageInfo"]["hasNextPage"]:
        if progress:
            print(f"  paginating {node['name']} ({history['totalCount']} commits)",
                  file=sys.stderr)
        dates = commit_dates(owner, node["name"], author, history, since)
    else:
        dates = [n["committedDate"] for n in history["nodes"]]

    minutes = sorted(
        int((datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
             - start).total_seconds() // 60)
        for d in dates)
    minutes = [m for m in minutes if m >= 0]

    languages = [{"name": e["node"]["name"], "bytes": e["size"]}
                 for e in node["languages"]["edges"]]
    primary = node.get("primaryLanguage") or {}
    everyone = (target.get("everyone") or {}).get("totalCount", 0)

    return {
        "name": node["name"],
        "full": node["nameWithOwner"],
        "url": node["url"],
        "description": node["description"] or "",
        "fork": node["isFork"],
        "archived": node["isArchived"],
        "private": node["isPrivate"],
        "disk": node["diskUsage"] or 0,
        "stars": node["stargazerCount"],
        "created": node["createdAt"],
        "pushed": node["pushedAt"],
        "language": primary.get("name") or "None",
        "languages": languages,
        "bytes": node["languages"]["totalSize"] or 0,
        "commits": len(minutes),
        "everyone": everyone,
        "at": minutes,
    }


TEMPLATE = r"""<!doctype html>
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
    --series:       #2a78d6;
    --ramp-0:       #86b6ef;
    --ramp-1:       #5598e7;
    --ramp-2:       #2a78d6;
    --ramp-3:       #1c5cab;
    --ramp-4:       #104281;
    --slot-1:       #2a78d6;
    --slot-2:       #eb6834;
    --slot-3:       #1baf7a;
    --slot-4:       #eda100;
    --slot-5:       #e87ba4;
    --slot-6:       #008300;
    --slot-7:       #4a3aa7;
    --slot-other:   #dcdbd3;
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
    --series:       #3987e5;
    --ramp-0:       #184f95;
    --ramp-1:       #256abf;
    --ramp-2:       #3987e5;
    --ramp-3:       #6da7ec;
    --ramp-4:       #9ec5f4;
    --slot-1:       #3987e5;
    --slot-2:       #d95926;
    --slot-3:       #199e70;
    --slot-4:       #c98500;
    --slot-5:       #d55181;
    --slot-6:       #008300;
    --slot-7:       #9085e9;
    --slot-other:   #46453f;
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
      --series:       #3987e5;
      --ramp-0:       #184f95;
      --ramp-1:       #256abf;
      --ramp-2:       #3987e5;
      --ramp-3:       #6da7ec;
      --ramp-4:       #9ec5f4;
      --slot-1:       #3987e5;
      --slot-2:       #d95926;
      --slot-3:       #199e70;
      --slot-4:       #c98500;
      --slot-5:       #d55181;
      --slot-6:       #008300;
      --slot-7:       #9085e9;
      --slot-other:   #46453f;
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

  .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0 18px; }
  .kpi {
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow);
  }
  .kpi .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  .kpi .value { font-size: 26px; font-weight: 600; margin-top: 2px; letter-spacing: -0.02em; }
  .kpi .note { color: var(--ink-2); font-size: 12px; }

  .filters {
    display: flex; flex-wrap: wrap; align-items: flex-end; gap: 14px 18px;
    padding: 12px 14px; margin-bottom: 18px;
    background: var(--surface); border: 1px solid var(--hairline); border-radius: 10px;
  }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field > span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  select {
    font: inherit; font-size: 13px; color: var(--ink); background: var(--surface);
    border: 1px solid var(--axis); border-radius: 6px; padding: 5px 8px; min-width: 150px;
  }
  .checks { display: flex; gap: 14px; align-items: center; padding-bottom: 5px; }
  .checks label { display: flex; gap: 6px; align-items: center; font-size: 13px; color: var(--ink-2); cursor: pointer; }

  .card {
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: 10px; padding: 16px 18px 18px; margin-bottom: 18px; box-shadow: var(--shadow);
  }
  .card h2 { font-size: 15px; font-weight: 600; margin: 0; }
  .card .caption { color: var(--muted); font-size: 12px; margin: 2px 0 12px; }

  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 0 0 12px; }
  .legend .item { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); }
  .legend .swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .legend .rule { width: 14px; height: 3px; border-radius: 2px; flex: none; }

  svg { display: block; width: 100%; overflow: visible; }
  svg[width] { width: auto; }
  svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; }
  .tile { cursor: pointer; outline: none; }
  .tile rect.focus { fill: none; stroke: none; }
  .tile:hover rect.focus { stroke: var(--ink); stroke-opacity: 0.5; stroke-width: 2; }
  .tile:focus-visible rect.focus { stroke: var(--ink); stroke-width: 2; }
  .row-label { fill: var(--ink-2); }
  .row-label.dim { fill: var(--muted); }
  .group-label, .axis-text { fill: var(--muted); }
  .value-text { fill: var(--ink-2); }
  .gridline { stroke: var(--grid); stroke-width: 1; }
  .crosshair { stroke: var(--axis); stroke-width: 1; }
  .cell { cursor: default; }
  .cell:hover rect, .cell:hover circle { stroke: var(--ink); stroke-opacity: 0.55; stroke-width: 2; }

  #tooltip {
    position: fixed; z-index: 20; pointer-events: none; opacity: 0;
    transition: opacity 80ms linear; max-width: 320px;
    background: var(--surface); color: var(--ink);
    border: 1px solid var(--hairline); border-radius: 8px;
    padding: 9px 11px; box-shadow: 0 4px 14px rgba(11,11,11,0.16);
  }
  #tooltip .t-name { font-weight: 600; font-size: 13px; }
  #tooltip .t-desc { color: var(--ink-2); font-size: 12px; margin-top: 2px; }
  #tooltip .t-row { display: flex; gap: 6px; align-items: baseline; font-size: 12px; margin-top: 3px; }
  #tooltip .t-val { font-weight: 600; font-variant-numeric: tabular-nums; }
  #tooltip .t-key { color: var(--ink-2); }
  #tooltip .t-key-line { width: 10px; height: 2px; border-radius: 1px; flex: none; align-self: center; }

  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--grid); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase;
       letter-spacing: 0.05em; cursor: pointer; white-space: nowrap; }
  th:hover { color: var(--ink); }
  th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td a { color: var(--ink); text-decoration: none; border-bottom: 1px solid var(--axis); }
  td a:hover { border-bottom-color: var(--ink); }
  td.flags { color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); padding: 28px 0; text-align: center; }
  .scroll { overflow-x: auto; }

  @media (max-width: 860px) {
    .kpis { grid-template-columns: repeat(2, 1fr); }
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

  <section class="filters">
    <label class="field"><span>Window</span>
      <select id="weeks"></select>
    </label>
    <div class="checks">
      <label><input type="checkbox" id="forks"> Forks</label>
      <label><input type="checkbox" id="private" checked> Private</label>
    </div>
    <span class="spacer"></span>
    <label class="field"><span>Treemap area</span>
      <select id="area">
        <option value="bytes">Code (bytes)</option>
        <option value="disk">Repo size on disk</option>
        <option value="commits">Commits in window</option>
      </select>
    </label>
    <label class="field"><span>Treemap grouping</span>
      <select id="group">
        <option value="language">Language</option>
        <option value="none">Nothing</option>
      </select>
    </label>
  </section>

  <section class="card">
    <h2>Where the weeks went</h2>
    <p class="caption" id="focus-caption"></p>
    <div class="legend" id="focus-legend"></div>
    <div id="focus"></div>
  </section>

  <section class="card">
    <h2>Every project, every week</h2>
    <p class="caption" id="map-caption"></p>
    <div class="legend" id="map-legend"></div>
    <div class="scroll"><div id="map"></div></div>
  </section>

  <section class="card">
    <h2>The hours it happens in</h2>
    <p class="caption" id="rhythm-caption"></p>
    <div class="scroll"><div id="rhythm"></div></div>
  </section>

  <section class="card">
    <h2 id="tm-title"></h2>
    <p class="caption" id="tm-caption"></p>
    <div class="legend" id="tm-legend"></div>
    <div id="treemap"></div>
  </section>

  <section class="card">
    <h2>Every project in this slice</h2>
    <p class="caption">Click a column to sort. The charts show no number this table leaves out.</p>
    <div class="scroll"><table id="table"><thead></thead><tbody></tbody></table></div>
  </section>
</div>

<div id="tooltip" role="tooltip" aria-hidden="true"></div>
<script type="application/json" id="viz-data">__DATA__</script>
<script>
__SCRIPT__
</script>
</body>
</html>
"""


SCRIPT = r"""
const DATA = JSON.parse(document.getElementById('viz-data').textContent);
const NS = 'http://www.w3.org/2000/svg';
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const WEEKS = DATA.weeks;
const START = Date.parse(DATA.windowStart);
const RAMP = ['--ramp-0', '--ramp-1', '--ramp-2', '--ramp-3', '--ramp-4'];
const RAMP_INK_LIGHT = ['#0b0b0b', '#0b0b0b', '#ffffff', '#ffffff', '#ffffff'];
const RAMP_INK_DARK = ['#ffffff', '#ffffff', '#ffffff', '#0b0b0b', '#0b0b0b'];
const SLOTS = ['--slot-1', '--slot-2', '--slot-3', '--slot-4', '--slot-5',
               '--slot-6', '--slot-7'];
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/* Band colour follows the project, fixed from the whole window, so changing
   the filters never repaints the projects that survive it. */
const BANDS = DATA.repos.filter(r => r.commits > 0)
  .sort((a, b) => b.commits - a.commits).slice(0, SLOTS.length)
  .map(r => r.name);

const state = {
  weeks: WEEKS, area: 'bytes', group: 'language',
  forks: false, private: true,
  sort: 'commits', desc: true,
};

/* ---------------------------------------------------------------- format */

function fmtInt(n) { return n.toLocaleString('en-US'); }

function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return v.toFixed(i === 0 ? 0 : (v < 10 ? 1 : 0)) + ' ' + units[i];
}

function fmtDisk(kb) { return fmtBytes(kb * 1024); }

function plural(n, one, many) { return fmtInt(n) + ' ' + (n === 1 ? one : many); }

function daysSince(iso) {
  if (!iso) return null;
  return Math.floor((Date.now() - Date.parse(iso)) / 86400000);
}

function fmtAgo(iso) {
  const d = daysSince(iso);
  if (d === null) return 'never';
  if (d <= 0) return 'today';
  if (d === 1) return 'yesterday';
  if (d < 21) return d + ' days ago';
  if (d < 60) return Math.round(d / 7) + ' weeks ago';
  if (d < 730) return Math.round(d / 30) + ' months ago';
  return (d / 365).toFixed(1) + ' years ago';
}

function weekStart(i) { return new Date(START + i * WEEK_MS); }

function weekLabel(i) {
  return weekStart(i).toLocaleDateString('en-US',
    { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function monthOf(i) {
  return weekStart(i).toLocaleDateString('en-US',
    { month: 'short', timeZone: 'UTC' });
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function isDark() {
  const stamped = document.documentElement.getAttribute('data-theme');
  if (stamped) return stamped === 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

/* -------------------------------------------------------------- the slice */

function weeklyOf(repo) {
  if (!repo.weekly) {
    const w = new Array(WEEKS).fill(0);
    for (const m of repo.at) {
      const i = Math.floor(m / (7 * 24 * 60));
      if (i >= 0 && i < WEEKS) w[i] += 1;
    }
    repo.weekly = w;
  }
  return repo.weekly;
}

function firstWeek() { return WEEKS - state.weeks; }

function slice() {
  const from = firstWeek();
  return DATA.repos
    .filter(r => (state.forks || !r.fork) &&
                 (state.private || !r.private))
    .map(r => {
      const weekly = weeklyOf(r).slice(from);
      return Object.assign(Object.create(r), {
        weekly: weekly,
        windowCommits: weekly.reduce((s, v) => s + v, 0),
        at: r.at.filter(m => m >= from * 7 * 24 * 60),
      });
    });
}

function bandColor(name) {
  const i = BANDS.indexOf(name);
  return cssVar(i < 0 ? '--slot-other' : SLOTS[i]);
}

const AREAS = {
  bytes: { label: 'code', value: r => r.bytes, fmt: fmtBytes },
  disk: { label: 'on disk', value: r => r.disk, fmt: fmtDisk },
  commits: { label: 'commits', value: r => r.windowCommits, fmt: fmtInt },
};

const HEAT = [
  { min: 1, label: '1' },
  { min: 2, label: '2 to 4' },
  { min: 5, label: '5 to 9' },
  { min: 10, label: '10 to 19' },
  { min: 20, label: '20 or more' },
];

function heatStep(n) {
  let step = -1;
  for (let i = 0; i < HEAT.length; i += 1) if (n >= HEAT[i].min) step = i;
  return step;
}

/* --------------------------------------------------------------- tooltip */

const tip = document.getElementById('tooltip');

function tipName(text) {
  const node = document.createElement('div');
  node.className = 't-name';
  node.textContent = text;
  return node;
}

function tipNote(text) {
  const node = document.createElement('div');
  node.className = 't-desc';
  node.textContent = text;
  return node;
}

function tipRow(value, key, color) {
  const row = document.createElement('div');
  row.className = 't-row';
  if (color) {
    const line = document.createElement('span');
    line.className = 't-key-line';
    line.style.background = color;
    row.appendChild(line);
  }
  const v = document.createElement('span');
  v.className = 't-val';
  v.textContent = value;
  const k = document.createElement('span');
  k.className = 't-key';
  k.textContent = key;
  row.append(v, k);
  return row;
}

function showTip(nodes, event) {
  tip.replaceChildren(...nodes);
  tip.style.opacity = '1';
  tip.setAttribute('aria-hidden', 'false');
  moveTip(event);
}

function moveTip(event) {
  const box = tip.getBoundingClientRect();
  let x = 14, y = 16;
  if (event && event.clientX) {
    x = event.clientX + 14;
    y = event.clientY + 16;
  } else if (event && event.target && event.target.getBoundingClientRect) {
    const r = event.target.getBoundingClientRect();
    x = r.left; y = r.bottom + 8;
  }
  tip.style.left = Math.max(8, Math.min(x, window.innerWidth - box.width - 12)) + 'px';
  tip.style.top = Math.max(8, Math.min(y, window.innerHeight - box.height - 12)) + 'px';
}

function hideTip() {
  tip.style.opacity = '0';
  tip.setAttribute('aria-hidden', 'true');
}

function hoverable(node, build) {
  node.addEventListener('pointerenter', e => showTip(build(), e));
  node.addEventListener('pointermove', moveTip);
  node.addEventListener('pointerleave', hideTip);
  node.addEventListener('focus', e => showTip(build(), e));
  node.addEventListener('blur', hideTip);
}

function repoTip(r) {
  const rows = [tipName(r.full)];
  if (r.description) rows.push(tipNote(r.description));
  rows.push(tipRow(plural(r.windowCommits, 'commit', 'commits'), 'you, in this window'));
  if (r.everyone > r.commits) {
    rows.push(tipRow(fmtInt(r.everyone - r.commits), 'commits by everyone else'));
  }
  rows.push(tipRow(fmtBytes(r.bytes), 'of code, ' + r.language));
  rows.push(tipRow(fmtDisk(r.disk), 'on disk'));
  rows.push(tipRow(fmtAgo(r.pushed), 'last push'));
  if (r.stars) rows.push(tipRow(fmtInt(r.stars), r.stars === 1 ? 'star' : 'stars'));
  const flags = [r.fork ? 'fork' : null, r.archived ? 'archived' : null,
                 r.private ? 'private' : null].filter(Boolean);
  if (flags.length) rows.push(tipRow(flags.join(', '), ''));
  return rows;
}

/* ------------------------------------------------------------------- svg */

function el(name, attrs) {
  const node = document.createElementNS(NS, name);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  return node;
}

function textNode(x, y, str, cls, extra) {
  const t = el('text', Object.assign({ x: x, y: y }, extra || {}));
  if (cls) t.setAttribute('class', cls);
  t.textContent = str;
  return t;
}

function textWidth(str, size) { return str.length * size * 0.56; }

function wrapName(name, maxW, size, maxLines) {
  if (textWidth(name, size) <= maxW) return [name];
  if (maxLines < 2) return null;
  const parts = [];
  let part = '';
  for (const ch of name) {
    part += ch;
    if (ch === '-' || ch === '.' || ch === '_') { parts.push(part); part = ''; }
  }
  if (part) parts.push(part);
  const lines = [];
  let line = '';
  for (const chunk of parts) {
    if (textWidth(chunk, size) > maxW) return null;
    if (line && textWidth(line + chunk, size) > maxW) { lines.push(line); line = chunk; }
    else line += chunk;
  }
  if (line) lines.push(line);
  return lines.length <= maxLines ? lines : null;
}

function svgFrame(host, height, width) {
  host.replaceChildren();
  const w = width || Math.max(280, host.clientWidth || host.parentElement.clientWidth);
  const svg = el('svg', { viewBox: '0 0 ' + w + ' ' + height, height: height });
  if (width) svg.setAttribute('width', width);
  host.appendChild(svg);
  return { svg: svg, width: w, height: height };
}

function roundedTop(x, y, w, h, r) {
  const rad = Math.min(r, w / 2, h);
  return 'M' + x + ' ' + (y + h) + ' V' + (y + rad) +
    ' Q' + x + ' ' + y + ' ' + (x + rad) + ' ' + y +
    ' H' + (x + w - rad) + ' Q' + (x + w) + ' ' + y + ' ' + (x + w) + ' ' + (y + rad) +
    ' V' + (y + h) + ' Z';
}

function niceMax(value) {
  if (value <= 0) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(value)));
  const scaled = value / pow;
  return (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10) * pow;
}
"""

SCRIPT += r"""
/* ------------------------------------------------------- where it went */

function focusSeries(rows) {
  const n = state.weeks;
  const series = [];
  for (const name of BANDS) {
    const repo = rows.find(r => r.name === name);
    if (repo && repo.windowCommits > 0) {
      series.push({ name: name, color: bandColor(name), values: repo.weekly });
    }
  }
  const rest = rows.filter(r => BANDS.indexOf(r.name) < 0 && r.windowCommits > 0);
  if (rest.length) {
    const values = new Array(n).fill(0);
    for (const r of rest) r.weekly.forEach((v, i) => { values[i] += v; });
    series.push({ name: 'Everything else', color: cssVar('--slot-other'),
                  values: values, count: rest.length });
  }
  return series;
}

function renderFocus(rows) {
  const host = document.getElementById('focus');
  const caption = document.getElementById('focus-caption');
  const legend = document.getElementById('focus-legend');
  const n = state.weeks;
  const series = focusSeries(rows);
  const totals = new Array(n).fill(0);
  for (const s of series) s.values.forEach((v, i) => { totals[i] += v; });
  const grand = totals.reduce((s, v) => s + v, 0);

  legend.replaceChildren();
  if (!grand) {
    host.replaceChildren();
    caption.textContent = '';
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'No commits of yours in this window.';
    host.appendChild(p);
    return;
  }

  const height = 260, padL = 42, padR = 8, padT = 14, padB = 26;
  const frame = svgFrame(host, height);
  const plotW = frame.width - padL - padR;
  const plotH = height - padT - padB;
  const top = niceMax(Math.max(...totals));
  const x = i => padL + (n === 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const y = v => padT + plotH - (v / top) * plotH;
  const surface = cssVar('--surface');

  [top, top / 2].forEach(tick => {
    frame.svg.appendChild(el('line', {
      x1: padL, x2: frame.width - padR, y1: y(tick), y2: y(tick), class: 'gridline',
    }));
    frame.svg.appendChild(textNode(padL - 7, y(tick) + 4, fmtInt(Math.round(tick)),
      'axis-text', { 'text-anchor': 'end' }));
  });

  let lower = new Array(n).fill(0);
  for (const s of series) {
    const upper = lower.map((v, i) => v + s.values[i]);
    const forward = upper.map((v, i) => x(i) + ' ' + y(v)).join(' L');
    const back = [];
    for (let i = n - 1; i >= 0; i -= 1) back.push(x(i) + ' ' + y(lower[i]));
    frame.svg.appendChild(el('path', {
      d: 'M' + forward + ' L' + back.join(' L') + ' Z', fill: s.color,
    }));
    frame.svg.appendChild(el('path', {
      d: 'M' + forward, fill: 'none', stroke: surface, 'stroke-width': 1.5,
    }));
    s.upper = upper;
    s.lower = lower;
    lower = upper;
  }

  let last = '';
  for (let i = 0; i < n; i += 1) {
    const month = monthOf(i + firstWeek());
    if (month === last) continue;
    last = month;
    frame.svg.appendChild(textNode(x(i), height - 8, month, 'axis-text',
      { 'text-anchor': 'middle' }));
  }

  const cross = el('line', { y1: padT, y2: padT + plotH, class: 'crosshair', opacity: 0 });
  frame.svg.appendChild(cross);
  const overlay = el('rect', {
    x: padL, y: padT, width: plotW, height: plotH, fill: 'transparent',
  });
  overlay.addEventListener('pointermove', event => {
    const box = frame.svg.getBoundingClientRect();
    const at = ((event.clientX - box.left) / box.width) * frame.width;
    const i = Math.max(0, Math.min(n - 1, Math.round(((at - padL) / plotW) * (n - 1))));
    cross.setAttribute('x1', x(i));
    cross.setAttribute('x2', x(i));
    cross.setAttribute('opacity', 1);
    const nodes = [tipName('Week of ' + weekLabel(i + firstWeek())),
                   tipRow(plural(totals[i], 'commit', 'commits'), 'in all')];
    for (const s of series) {
      if (s.values[i] > 0) nodes.push(tipRow(fmtInt(s.values[i]), s.name, s.color));
    }
    showTip(nodes, event);
  });
  overlay.addEventListener('pointerleave', () => {
    cross.setAttribute('opacity', 0);
    hideTip();
  });
  frame.svg.appendChild(overlay);

  for (const s of series) {
    const item = document.createElement('span');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = s.color;
    const text = document.createElement('span');
    text.textContent = s.count ? s.name + ' (' + s.count + ')' : s.name;
    item.append(sw, text);
    legend.appendChild(item);
  }

  const busiest = totals.indexOf(Math.max(...totals));
  caption.textContent = plural(grand, 'commit', 'commits') + ' you authored over ' +
    plural(n, 'week', 'weeks') + ', stacked by project. Busiest week of ' +
    weekLabel(busiest + firstWeek()) + ', at ' + fmtInt(totals[busiest]) + '.';
}
"""

SCRIPT += r"""
/* ---------------------------------------------------------- attention map */

function renderMap(rows) {
  const host = document.getElementById('map');
  const caption = document.getElementById('map-caption');
  const legend = document.getElementById('map-legend');
  const n = state.weeks;
  const live = rows.filter(r => r.windowCommits > 0).map(r => {
    let last = -1, weighted = 0;
    r.weekly.forEach((v, i) => { if (v > 0) last = i; weighted += v * i; });
    /* The commit-weighted mean week, so a project sorts by where its work
       actually sits rather than by a single trailing commit. */
    return Object.assign(Object.create(r),
      { lastWeek: last, centre: weighted / r.windowCommits });
  }).sort((a, b) => b.centre - a.centre || b.windowCommits - a.windowCommits);

  legend.replaceChildren();
  if (!live.length) {
    host.replaceChildren();
    caption.textContent = '';
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'No commits of yours in this window.';
    host.appendChild(p);
    return;
  }

  const longest = live.reduce((w, r) => Math.max(w, textWidth(r.name, 11)), 0);
  const labelW = Math.min(220, Math.max(96, longest + 14));
  const avail = Math.max(280, host.parentElement.clientWidth) - labelW - 4;
  const cell = Math.max(8, Math.min(22, avail / n));
  const gap = cell > 12 ? 2 : 1;
  const rowH = Math.max(13, Math.min(20, cell));
  const headH = 18;
  const width = labelW + cell * n + 4;
  const frame = svgFrame(host, headH + live.length * rowH + 6, width);
  const shades = RAMP.map(cssVar);
  const empty = cssVar('--empty');

  let last = '';
  for (let i = 0; i < n; i += 1) {
    const month = monthOf(i + firstWeek());
    if (month === last) continue;
    last = month;
    frame.svg.appendChild(textNode(labelW + i * cell, headH - 6, month, 'axis-text'));
  }

  live.forEach((repo, row) => {
    const y = headH + row * rowH;
    const quiet = repo.lastWeek < n - 4;
    frame.svg.appendChild(textNode(labelW - 8, y + rowH / 2 + 3, repo.name,
      quiet ? 'row-label dim' : 'row-label', { 'text-anchor': 'end' }));
    for (let i = 0; i < n; i += 1) {
      const count = repo.weekly[i];
      const step = heatStep(count);
      const g = el('g', { class: 'cell' });
      g.appendChild(el('rect', {
        x: labelW + i * cell, y: y + (rowH - (rowH - gap)) / 2,
        width: Math.max(2, cell - gap), height: rowH - gap, rx: 2,
        fill: step < 0 ? empty : shades[step],
      }));
      hoverable(g, () => [
        tipName(repo.name),
        tipRow(count ? plural(count, 'commit', 'commits') : 'nothing',
               'week of ' + weekLabel(i + firstWeek())),
      ]);
      frame.svg.appendChild(g);
    }
  });

  HEAT.forEach((bin, i) => {
    const item = document.createElement('span');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = shades[i];
    const text = document.createElement('span');
    text.textContent = bin.label;
    item.append(sw, text);
    legend.appendChild(item);
  });

  const quiet = live.filter(r => r.lastWeek < n - 4).length;
  caption.textContent = 'One row per project, one cell per week, ordered so the ' +
    'projects whose commits sit latest in the window rise to the top. ' +
    plural(live.length, 'project', 'projects') + ' saw a commit here' +
    (quiet ? '; the ' + quiet + ' greyed out have been quiet for a month or more.' : '.');
}

/* ---------------------------------------------------------------- rhythm */

function renderRhythm(rows) {
  const host = document.getElementById('rhythm');
  const caption = document.getElementById('rhythm-caption');
  const grid = DAYS.map(() => new Array(24).fill(0));
  let total = 0;
  for (const repo of rows) {
    for (const m of repo.at) {
      const when = new Date(START + m * 60000);
      grid[(when.getDay() + 6) % 7][when.getHours()] += 1;
      total += 1;
    }
  }

  if (!total) {
    host.replaceChildren();
    caption.textContent = '';
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'No commits of yours in this window.';
    host.appendChild(p);
    return;
  }

  const labelW = 40, headH = 16;
  const avail = Math.max(280, host.parentElement.clientWidth) - labelW - 4;
  const cell = Math.max(16, Math.min(34, avail / 24));
  const width = labelW + cell * 24 + 4;
  const frame = svgFrame(host, headH + 7 * cell + 18, width);
  const max = Math.max(...grid.flat());
  const series = cssVar('--series');

  for (let hour = 0; hour < 24; hour += 3) {
    frame.svg.appendChild(textNode(labelW + (hour + 0.5) * cell, headH - 5,
      (hour % 12 === 0 ? 12 : hour % 12) + (hour < 12 ? 'a' : 'p'), 'axis-text',
      { 'text-anchor': 'middle' }));
  }

  DAYS.forEach((day, row) => {
    const y = headH + row * cell;
    frame.svg.appendChild(textNode(labelW - 9, y + cell / 2 + 4, day, 'row-label',
      { 'text-anchor': 'end' }));
    for (let hour = 0; hour < 24; hour += 1) {
      const count = grid[row][hour];
      const g = el('g', { class: 'cell' });
      const cx = labelW + (hour + 0.5) * cell;
      const cy = y + cell / 2;
      if (count > 0) {
        g.appendChild(el('circle', {
          cx: cx, cy: cy, r: 2 + (cell / 2 - 3) * Math.sqrt(count / max), fill: series,
        }));
      } else {
        g.appendChild(el('circle', { cx: cx, cy: cy, r: 1.5, fill: cssVar('--empty') }));
      }
      g.appendChild(el('rect', {
        x: labelW + hour * cell, y: y, width: cell, height: cell, fill: 'transparent',
      }));
      hoverable(g, () => [
        tipName(day + ', ' + (hour % 12 === 0 ? 12 : hour % 12) +
                (hour < 12 ? 'am' : 'pm')),
        tipRow(count ? plural(count, 'commit', 'commits') : 'nothing', '', series),
      ]);
      frame.svg.appendChild(g);
    }
  });

  let peakDay = 0, peakHour = 0;
  grid.forEach((day, d) => day.forEach((v, h) => {
    if (v === max) { peakDay = d; peakHour = h; }
  }));
  const weekend = grid[5].concat(grid[6]).reduce((s, v) => s + v, 0);
  const offHours = grid.reduce((s, day) => s + day.reduce(
    (t, v, h) => t + (h < 9 || h >= 18 ? v : 0), 0), 0);
  caption.textContent = 'Every commit placed in your local week. Busiest at ' +
    DAYS[peakDay] + ' ' + (peakHour % 12 === 0 ? 12 : peakHour % 12) +
    (peakHour < 12 ? 'am' : 'pm') + '. ' +
    Math.round((offHours / total) * 100) + '% land outside 9 to 6, ' +
    Math.round((weekend / total) * 100) + '% at the weekend.';
}
"""

SCRIPT += r"""
/* --------------------------------------------------------------- treemap */

const TM_BINS = [
  { min: 0, label: 'None' }, { min: 1, label: '1 to 9' }, { min: 10, label: '10 to 49' },
  { min: 50, label: '50 to 199' }, { min: 200, label: '200 or more' },
];

function tmStep(n) {
  let step = 0;
  for (let i = 0; i < TM_BINS.length; i += 1) if (n >= TM_BINS[i].min) step = i;
  return step;
}

function worst(row, side) {
  let sum = 0, min = Infinity, max = 0;
  for (const r of row) {
    sum += r.area;
    if (r.area < min) min = r.area;
    if (r.area > max) max = r.area;
  }
  if (sum <= 0 || side <= 0) return Infinity;
  const s2 = sum * sum, side2 = side * side;
  return Math.max((side2 * max) / s2, s2 / (side2 * min));
}

function placeRow(row, free, out) {
  const sum = row.reduce((s, r) => s + r.area, 0);
  if (free.w >= free.h) {
    const t = free.h > 0 ? Math.min(free.w, sum / free.h) : 0;
    let y = free.y;
    for (const r of row) {
      const h = t > 0 ? r.area / t : 0;
      out.push({ item: r.item, x: free.x, y: y, w: t, h: h });
      y += h;
    }
    return { x: free.x + t, y: free.y, w: free.w - t, h: free.h };
  }
  const t = free.w > 0 ? Math.min(free.h, sum / free.w) : 0;
  let x = free.x;
  for (const r of row) {
    const w = t > 0 ? r.area / t : 0;
    out.push({ item: r.item, x: x, y: free.y, w: w, h: t });
    x += w;
  }
  return { x: free.x, y: free.y + t, w: free.w, h: free.h - t };
}

function squarify(items, rect) {
  const out = [];
  const total = items.reduce((s, i) => s + Math.max(0, i.value), 0);
  if (total <= 0 || rect.w <= 0 || rect.h <= 0) return out;
  const scale = (rect.w * rect.h) / total;
  const queue = items.filter(i => i.value > 0)
    .map(i => ({ item: i, area: i.value * scale }))
    .sort((a, b) => b.area - a.area);
  let free = { x: rect.x, y: rect.y, w: rect.w, h: rect.h };
  let row = [];
  while (queue.length) {
    const side = Math.min(free.w, free.h);
    if (row.length === 0 || worst(row.concat([queue[0]]), side) <= worst(row, side)) {
      row.push(queue.shift());
    } else {
      free = placeRow(row, free, out);
      row = [];
    }
  }
  if (row.length) placeRow(row, free, out);
  return out;
}

function renderTreemap(rows) {
  const area = AREAS[state.area];
  const host = document.getElementById('treemap');
  const height = Math.round(Math.max(340, Math.min(560,
    (host.clientWidth || 900) * 0.46)));
  const frame = svgFrame(host, height);
  const inks = isDark() ? RAMP_INK_DARK : RAMP_INK_LIGHT;
  const shades = RAMP.map(cssVar);

  const plotted = rows.filter(r => area.value(r) > 0);
  const legend = document.getElementById('tm-legend');
  legend.replaceChildren();
  if (!plotted.length) {
    host.replaceChildren();
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = 'No project in this slice has a value for ' + area.label + '.';
    host.appendChild(p);
    return;
  }

  let boxes = [];
  if (state.group === 'language') {
    const byLang = new Map();
    for (const r of plotted) {
      if (!byLang.has(r.language)) byLang.set(r.language, []);
      byLang.get(r.language).push(r);
    }
    const groups = [...byLang.entries()].map(([name, members]) => ({
      name: name, members: members,
      value: members.reduce((s, r) => s + area.value(r), 0),
    }));
    for (const cell of squarify(groups, { x: 0, y: 0, w: frame.width, h: height })) {
      const g = cell.item;
      const header = cell.h >= 42 && cell.w >= 72 ? 16 : 2;
      if (header === 16) {
        const full = g.name + '  ' + area.fmt(g.value);
        const label = textWidth(full, 11) <= cell.w - 6 ? full : g.name;
        if (textWidth(label, 11) <= cell.w - 6) {
          frame.svg.appendChild(textNode(cell.x + 2, cell.y + 11, label, 'group-label'));
        }
      }
      boxes = boxes.concat(squarify(
        g.members.map(r => ({ repo: r, value: area.value(r) })),
        { x: cell.x, y: cell.y + header, w: cell.w, h: cell.h - header }));
    }
  } else {
    boxes = squarify(plotted.map(r => ({ repo: r, value: area.value(r) })),
                     { x: 0, y: 0, w: frame.width, h: height });
  }

  for (const box of boxes) {
    const r = box.item.repo;
    const x = box.x + 1, y = box.y + 1, w = box.w - 2, h = box.h - 2;
    if (w < 1.5 || h < 1.5) continue;
    const step = tmStep(r.windowCommits);
    const ink = inks[step];

    const link = el('a', { href: r.url, target: '_blank', rel: 'noopener' });
    link.setAttribute('class', 'tile');
    const title = el('title');
    title.textContent = r.full + ' — ' + area.fmt(area.value(r)) + ' ' + area.label +
      ', ' + plural(r.windowCommits, 'commit', 'commits') + ' in the window';
    link.appendChild(title);
    const rx = Math.min(3, w / 2, h / 2);
    link.appendChild(el('rect', {
      x: x, y: y, width: w, height: h, rx: rx, fill: shades[step], class: 'fill',
    }));
    link.appendChild(el('rect', { x: x, y: y, width: w, height: h, rx: rx, class: 'focus' }));

    const lines = w >= 44 && h >= 16
      ? wrapName(r.name, w - 10, 11, Math.min(3, Math.floor((h - 4) / 13)))
      : null;
    if (lines) {
      lines.forEach((line, i) => link.appendChild(
        textNode(x + 5, y + 13 + i * 13, line, null, { fill: ink, 'font-weight': '600' })));
      const value = area.fmt(area.value(r));
      const valueY = y + 13 + (lines.length - 1) * 13 + 14;
      if (valueY - y <= h - 4 && textWidth(value, 10.5) <= w - 10) {
        link.appendChild(textNode(x + 5, valueY, value, null,
          { fill: ink, 'font-size': '10.5', 'fill-opacity': '0.78' }));
      }
    }
    hoverable(link, () => repoTip(r));
    frame.svg.appendChild(link);
  }

  TM_BINS.forEach((bin, i) => {
    const item = document.createElement('span');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = shades[i];
    const text = document.createElement('span');
    text.textContent = bin.label;
    item.append(sw, text);
    legend.appendChild(item);
  });

  const dropped = rows.length - plotted.length;
  document.getElementById('tm-title').textContent = 'What it all adds up to';
  document.getElementById('tm-caption').textContent =
    'Tile area is ' + area.label + ', fill is commits in the window, grouped by ' +
    (state.group === 'language' ? 'primary language' : 'nothing') +
    '. Click a tile to open it on GitHub.' +
    (dropped ? ' ' + dropped + ' with no ' + area.label +
       ' can only be reached in the table.' : '');
}
"""

SCRIPT += r"""
/* ------------------------------------------------------------------ kpis */

function kpi(label, value, note) {
  const box = document.createElement('div');
  box.className = 'kpi';
  const l = document.createElement('div');
  l.className = 'label';
  l.textContent = label;
  const v = document.createElement('div');
  v.className = 'value';
  v.textContent = value;
  const n = document.createElement('div');
  n.className = 'note';
  n.textContent = note;
  box.append(l, v, n);
  return box;
}

function renderKpis(rows) {
  const n = state.weeks;
  const totals = new Array(n).fill(0);
  for (const r of rows) r.weekly.forEach((v, i) => { totals[i] += v; });
  const commits = totals.reduce((s, v) => s + v, 0);
  const touched = rows.filter(r => r.windowCommits > 0).length;
  const busiest = totals.indexOf(Math.max(...totals));

  let run = 0, best = 0, bestEnd = 0;
  totals.forEach((v, i) => {
    run = v > 0 ? run + 1 : 0;
    if (run > best) { best = run; bestEnd = i; }
  });

  document.getElementById('kpis').replaceChildren(
    kpi('Commits', fmtInt(commits), 'yours, across ' + plural(n, 'week', 'weeks')),
    kpi('Projects touched', fmtInt(touched),
        fmtInt(rows.length - touched) + ' sat still'),
    kpi('Busiest week', commits ? fmtInt(totals[busiest]) : '0',
        commits ? 'week of ' + weekLabel(busiest + firstWeek()) : 'nothing here'),
    kpi('Longest run', best ? plural(best, 'week', 'weeks') : 'none',
        best ? 'ending ' + weekLabel(bestEnd + firstWeek()) : 'no commits'));
}

/* ----------------------------------------------------------------- table */

function lastCommit(r) { return r.at.length ? START + r.at[r.at.length - 1] * 60000 : 0; }

const COLUMNS = [
  { key: 'name', label: 'Project', get: r => r.name },
  { key: 'language', label: 'Language', get: r => r.language },
  { key: 'commits', label: 'Commits', num: true, get: r => r.windowCommits, fmt: fmtInt },
  { key: 'last', label: 'Last commit', num: true, get: lastCommit,
    fmt: v => (v ? fmtAgo(new Date(v).toISOString()) : 'none here') },
  { key: 'bytes', label: 'Code', num: true, get: r => r.bytes, fmt: fmtBytes },
  { key: 'disk', label: 'On disk', num: true, get: r => r.disk, fmt: fmtDisk },
  { key: 'stars', label: 'Stars', num: true, get: r => r.stars, fmt: fmtInt },
  { key: 'flags', label: '', get: r => '',
    fmt: (_, r) => [r.fork ? 'fork' : null, r.archived ? 'archived' : null,
                    r.private ? 'private' : null].filter(Boolean).join(', ') },
];

function renderTable(rows) {
  const table = document.getElementById('table');
  const head = document.createElement('tr');
  for (const col of COLUMNS) {
    const th = document.createElement('th');
    th.className = col.num ? 'num' : '';
    th.textContent = col.label + (state.sort === col.key ? (state.desc ? ' ▾' : ' ▴') : '');
    th.addEventListener('click', () => {
      if (state.sort === col.key) state.desc = !state.desc;
      else { state.sort = col.key; state.desc = !!col.num; }
      render();
    });
    head.appendChild(th);
  }
  table.tHead.replaceChildren(head);

  const col = COLUMNS.find(c => c.key === state.sort) || COLUMNS[2];
  const sorted = [...rows].sort((a, b) => {
    const x = col.get(a), y = col.get(b);
    const cmp = typeof x === 'string' ? x.localeCompare(y) : x - y;
    return state.desc ? -cmp : cmp;
  });

  const body = document.createElement('tbody');
  for (const r of sorted) {
    const tr = document.createElement('tr');
    for (const c of COLUMNS) {
      const td = document.createElement('td');
      td.className = c.num ? 'num' : (c.key === 'flags' ? 'flags' : '');
      if (c.key === 'name') {
        const a = document.createElement('a');
        a.href = r.url;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = r.name;
        td.appendChild(a);
      } else {
        td.textContent = c.fmt ? c.fmt(c.get(r), r) : c.get(r);
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  table.replaceChild(body, table.tBodies[0]);
}

/* ---------------------------------------------------------------- render */

function render() {
  const rows = slice();
  renderKpis(rows);
  renderFocus(rows);
  renderMap(rows);
  renderRhythm(rows);
  renderTreemap(rows);
  renderTable(rows);
}

function bind(id, key, cast) {
  const node = document.getElementById(id);
  if (node.type === 'checkbox') node.checked = state[key];
  else node.value = state[key];
  node.addEventListener('change', () => {
    state[key] = node.type === 'checkbox' ? node.checked
      : (cast ? cast(node.value) : node.value);
    render();
  });
}

const windows = document.getElementById('weeks');
for (const weeks of [13, 26, 52, WEEKS].filter((w, i, all) =>
     w <= WEEKS && all.indexOf(w) === i)) {
  const option = document.createElement('option');
  option.value = weeks;
  option.textContent = weeks === WEEKS
    ? 'Everything read (' + weeks + ' weeks)'
    : 'Past ' + Math.round(weeks * 12 / 52) + ' months';
  windows.appendChild(option);
}

document.getElementById('title').textContent = DATA.owner;
document.getElementById('generated').textContent =
  'commits you authored, ' + weekLabel(0) + ' onward, read ' + DATA.generated;

bind('weeks', 'weeks', Number);
bind('area', 'area');
bind('group', 'group');
bind('forks', 'forks');
bind('private', 'private');

document.getElementById('theme').addEventListener('click', () => {
  document.documentElement.setAttribute('data-theme', isDark() ? 'light' : 'dark');
  render();
});
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(render, 120);
});

render();
"""


def build_html(payload):
    body = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    title = f"{payload['owner']} - where the attention went"
    return (TEMPLATE
            .replace("__TITLE__", title)
            .replace("__DATA__", body)
            .replace("__SCRIPT__", SCRIPT))


def default_output_dir():
    """Beside the script, so a run from any directory lands in the same place."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


def node_id(owner):
    return gh(["api", f"users/{owner}", "--jq", ".node_id"]).strip()


def main():
    parser = argparse.ArgumentParser(
        description="Chart where your attention went across your GitHub projects.")
    parser.add_argument("--owner", help="GitHub account (default: the gh-authenticated user)")
    parser.add_argument("--months", type=int, default=12,
                        help="how far back to read (default 12)")
    parser.add_argument("--out", help="where to write the page "
                                      "(default: output/repo-viz.html beside this script)")
    parser.add_argument("--json", action="store_true",
                        help="write the gathered data as JSON instead of a page")
    parser.add_argument("--no-open", action="store_true", help="do not open the page")
    parser.add_argument("--no-ignore", action="store_true",
                        help=f"scan every repo, disregarding {IGNORE_FILE}")
    args = parser.parse_args()

    try:
        ignore = load_ignore(enabled=not args.no_ignore)
    except IgnoreError as err:
        print(str(err), file=sys.stderr)
        return 1

    try:
        owner = args.owner or gh(["api", "user", "--jq", ".login"]).strip()
        author = node_id(owner)
        start, weeks = window(datetime.now(timezone.utc), args.months)
        print(f"reading {owner} from {start:%Y-%m-%d} ({weeks} weeks)...", file=sys.stderr)
        payload = gather(owner, author, start, weeks, ignore=ignore)
    except GhError as err:
        print(f"gh: {err}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    out = os.path.abspath(args.out or os.path.join(default_output_dir(), "repo-viz.html"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(build_html(payload))
    commits = sum(r["commits"] for r in payload["repos"])
    print(f"{commits} commits across {len(payload['repos'])} projects -> {out}",
          file=sys.stderr)
    if not args.no_open:
        webbrowser.open(f"file://{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
