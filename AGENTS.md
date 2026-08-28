# AGENTS.md

## What this repo is

`chris-peterson/chris-peterson` is a GitHub profile repo

| Path | What it is |
| --- | --- |
| `README.md` | Rendered on the GitHub profile page. Points at the docs site. |
| `repo-status.py` | A cross-repo dashboard and reconciler for every project under the account |
| `repo-viz.py` | A one-page chart of where attention went across those projects, week by week |
| `ignore.yml` | The repos a scan skips, read by both scripts |
| `requirements.txt` | PyYAML, the only dependency either script has |
| `.claude/commands/` | `/repo-status` and `/repo-viz`, each a thin wrapper mapping free-form arguments onto the script's flags |

## repo-status.py

Driven entirely through the `gh` CLI and `git`, with PyYAML the only import
outside the standard library. Run it from anywhere; it defaults to the
authenticated user and a clone tree at `~/src/github/<owner>`.

```bash
./repo-status.py                          # all eight sections, read-only
./repo-status.py --only uncommitted       # one section
./repo-status.py --skip issues --skip prs
./repo-status.py --fix                    # reconcile, prompting before each delete
```

Sections print in a fixed order, each one independently selectable with
`--only` / `--skip`: `reconcile`, `uncommitted`, `local-branches`,
`orphan-branches`, `prs`, `unreleased`, `issues`, `behind`.

### The order they print in

The middle six run in the order the work gets done: find what only your disk
holds, delete the branches that have served their purpose, audit the open PRs,
see what has piled up since the last release, then the issues. The two
clone-tree housekeeping passes bracket them. `reconcile` leads because the rest
of the run reads the tree it repairs — with `--fix` it moves and clones before
anything else probes. `behind` trails because a clone trailing origin costs
nothing until you go to work in it, and `--fix` fast-forwards the clean ones
without being asked.

### Uncommitted and unpushed

`uncommitted` is the one section about work that would be lost if the disk
were. It counts a dirty working tree, and for **every** local branch — not just
the checked-out one — the commits no remote holds, in three flavours:

| Flavour | How it is measured |
| --- | --- |
| ahead of a live upstream | the `ahead N` in `%(upstream:track)` |
| upstream deleted | `rev-list --count <branch> --not --remotes` |
| never had an upstream | the same count |

That last measure is what makes the section trustworthy on branches git has
nothing to compare against, and it is what `local-branches` consults before it
offers a `branch -D`: a branch whose remote is gone but whose commits live
nowhere else is held back from the batch and reported as held, with the count.

### The layout it enforces

Every clone belongs at `<root>/<repo name>`, flat. Grouping directories
(`forge/pwsh-github`) and forks filed under the upstream's name
(`dotnet/sdk` for a clone of `chris-peterson/sdk`) are findings, not layouts —
`reconcile` reports them and `--fix` moves them onto their own path, then
removes the grouping directory it just emptied.

### The two halves

Remote state comes from the GitHub API and reflects the server. Local state
comes from the clones under `--root`, each fetched with `--prune` first. Sections
that need both join them on the lowercased `owner/name` parsed out of each
clone's `origin` URL — never on the directory name, since the whole point of the
`reconcile` pass is that the directory may be in the wrong place or named after
a repo that has since been renamed.

`--depth` bounds the clone search (default 3) and the walk stops descending once
it finds a `.git`. The depth exists to catch strays, not to bless them.

Pure case differences between a directory and its repo name are left alone: the
macOS filesystem is case-insensitive, so `powershell` -> `PowerShell` isn't a
move a single `mv` can make.

### What --fix will and won't do

Without `--fix` nothing is written; every actionable finding prints the exact
command instead, ready to paste. With `--fix`:

- Clones missing repos and fast-forwards clean behind branches without asking.
- Asks y/n before anything that moves or removes — relocating a clone onto its
  own path, deleting a clone whose repo 404s, deleting a repo's merged local
  branches, deleting a repo's orphaned remote branches. `--yes` skips the asking.
- Batches deletions per repo, so one prompt and one `git push --delete` cover
  all of a repo's orphaned branches rather than dozens of round trips.

A fast-forward is attempted only when the branch is clean, has no unpushed
commits, and has an upstream. The default branch gets repaired even when it
isn't checked out, via `git fetch origin <default>:<default>` — a refspec fetch
moves the ref and refuses anything that isn't a fast-forward, which is the
safety property a `merge` couldn't give from another branch.

### Adding a section

A section is a `section_*` function that takes already-gathered state and
prints. Add its name to `SECTIONS`, gather whatever remote data it needs inside
`probe_repo` behind a `wanted` check so unselected sections cost no API calls,
and call it from `main` in report order.

Wrap each probe in `attempt("<area>", …)` and have the section print
`r["errors"]["<area>"]` itself. A repo can answer some endpoints and 404 on
others (pull requests turned off is the common case), so the failure has to
reach the section that asked for it and leave the rest of the report alone.

## ignore.yml

`ignore.yml` names what a scan skips, so a repo you have stopped caring about
stops costing API calls on every run. Both scripts read it from their own
directory rather than the working directory, and `--no-ignore` scans everything
anyway.

```yaml
archived: true      # skip archived repos
repos:              # skipped by name, whatever their state
  - home-tech
```

Each script loads it with `yaml.safe_load` and then **rejects anything the two
keys don't cover**, down to an unrecognized key: a line dropped in silence
would widen a scan without saying so, which is the one failure an ignore list
cannot have. Both scripts carry their own copy of that loader, so a change to
one belongs in the other too.

Where the two disagree, the more specific instruction wins:
`repo-status.py --repo home-tech` probes an ignored repo because you named it,
and `--include-archived` outranks `archived: true`.

The skip has to happen before the expensive work, not after. In `repo-viz.py`
that means filtering inside `gather` before `shape`, which paginates a repo's
whole commit history when one page won't hold it. In `repo-status.py` it means
dropping an ignored repo's clone alongside its remote record, so `reconcile`
doesn't spend an API call classifying a clone whose repo it just filtered out.
Each script prints what it skipped.

## repo-viz.py

The same account read as attention rather than as a worklist: what you have been
pouring your weeks into, and what has gone quiet. Driven through the `gh` CLI,
with PyYAML the only import outside the standard library. It writes a
self-contained HTML page — no CDN, no build step, no network at view time.

```bash
./repo-viz.py                       # write repo-viz.html and open it
./repo-viz.py --months 24           # widen the window
./repo-viz.py --owner some-user     # someone else's public projects
./repo-viz.py --out /tmp/x.html --no-open
./repo-viz.py --json                # the gathered data, for another tool
```

### Only the owner's own commits count

Every history query is filtered by the owner's user node id. A fork tracking
upstream lands hundreds of commits on its default branch that were never
anyone's attention — `chris-peterson/PowerShell` carries 430 in a year, none of
them his — and unfiltered they bury the work that was. The unfiltered count is
kept per repo as `everyone` so a tooltip can say how much of the branch is
someone else's.

### What it reads

One GraphQL query per page of 50 repos carries everything the charts plot. A
repo whose window holds more than one page of commits gets its history
paginated in full, so the weekly buckets are exact rather than capped at the
first hundred. Each commit is stored as minutes since the window opened, which
keeps the payload small and lets the page place commits in the *viewer's*
timezone rather than a baked-in one.

The window is whole weeks, starting on a Monday at midnight UTC, so a week
index is plain division and every bucket is the same width.

### What it draws

Five views of one slice, scoped by a single filter row (window length, and
forks and private repos each toggle). Archived repos are excluded by
`ignore.yml` rather than by a toggle, so `--no-ignore` is what brings them
back, flagged `archived` on the tile and in the table:

- **focus** — weekly commits as a stacked area, one band per project. How much
  energy, and where it went.
- **map** — every project against every week. Rows sort by the commit-weighted
  mean week, so a project sits where its work actually falls rather than where
  a single trailing commit does; the result is a diagonal, and reading down it
  is reading the order attention moved in.
- **rhythm** — a weekday × hour punch card in the viewer's local time.
- **treemap** — what the work amounts to: area is size, fill is commits in the
  window, grouped by primary language.
- **table** — every number the charts show, sortable.

### The encoding rules it holds to

Band colour is assigned once from the whole window, not per slice, so changing
a filter never repaints the projects that survive it. Seven projects hold
categorical slots and the rest fold into one receding grey — the eighth slot is
where the palette's adjacent-pair separation starts to fail, and a generated
hue would fail it outright.

Heat is an ordinal blue ramp and never carries identity: a treemap and a
heatmap both put arbitrary neighbours side by side, which is the case a
categorical palette cannot survive past three hues. The dark ramps are their
own steps against the dark surface rather than an inversion, and tile ink flips
with the step so text on a fill always clears contrast.

A label that will not fit is wrapped on its `-` and `.` boundaries and dropped
if it still will not fit; nothing is ever clipped. Tiles whose area metric is
zero can't be drawn, so the treemap caption counts them and the table keeps
them reachable.
