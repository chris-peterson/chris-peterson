# AGENTS.md

## What this repo is

`chris-peterson/chris-peterson` is a GitHub profile repo

| Path | What it is |
| --- | --- |
| `README.md` | Rendered on the GitHub profile page. Points at the docs site. |
| `justfile` | The front door. Bare `just` lists what there is to run; `just check` verifies what a run needs. |
| `repo-status.py` | A cross-repo dashboard and reconciler for every project under the account |
| `repo-viz.py` | A one-page chart of where attention went across those projects, week by week |
| `ignore.yml` | The repos a scan skips, read by both scripts |
| `maturity.yml` | The bar every project is measured against, in cumulative tiers |
| `maturity-history.yml` | Where each project's tier has been, appended to when one moves |
| `output/` | Where both scripts write their pages. Gitignored. |
| `requirements.txt` | PyYAML, the only dependency either script has |
| `.claude/commands/` | `/repo-status` and `/repo-viz`, each a thin wrapper mapping free-form arguments onto the script's flags |
| `.claude/skills/` | `ratchet` and `triage-alerts`, which close what the report only measures |

## justfile

Bare `just` lists rather than runs: what the repo is, then every recipe under
the question it answers — `start here`, `what is on the board`, `act on it`,
`the pages`. Groups print in source order, so the file's order is the reading
order.

```bash
just check                # gh, its auth, PyYAML, the clone tree, and both configs parsing
just demo                 # one project, narrated end to end
just repo moor            # one project, leaving the tier ledger alone
just section unreleased   # one section; `just sections` names them
just fix                  # the only recipe that writes
```

Every recipe takes the script's own flags after its arguments
(`just status --all-details`), and the ones that narrow a run are dependencies
on `status` rather than copies of it, so there is one place the command is
built. `just check` is the only recipe that reaches into the scripts: it calls
their loaders directly, so a config the run would reject is reported before the
run costs an API call.

## repo-status.py

Driven entirely through the `gh` CLI and `git`, with PyYAML the only import
outside the standard library. Run it from anywhere; it defaults to the
authenticated user and a clone tree at `~/src/github/<owner>`.

```bash
./repo-status.py                          # all sections, read-only
./repo-status.py --only uncommitted       # one section
./repo-status.py --skip issues --skip prs
./repo-status.py --fix                    # reconcile, prompting before each delete
./repo-status.py --open                   # open the page the run wrote
```

Sections print in a fixed order, each one independently selectable with
`--only` / `--skip`: `upstream`, `reconcile`, `uncommitted`, `local-branches`,
`orphan-branches`, `prs`, `unreleased`, `issues`, `alerts`, `maturity`,
`behind`.

### The order they print in

`upstream` leads because it is the only section about work you cannot finish
yourself. Everything below it waits on you; an upstream pull request waits on a
maintainer, and the clock has been running since you opened it.

The middle eight run in the order the work gets done: find what only your disk
holds, delete the branches that have served their purpose, audit the open PRs,
see what has piled up since the last release, then the issues, then what the
security feeds have turned up, then the standing bar every project is held to.
The two clone-tree housekeeping passes bracket them. `reconcile` opens them
because the rest of the run reads the tree it repairs — with `--fix` it moves
and clones before anything else probes. `behind` trails because a clone
trailing origin costs nothing until you go to work in it, and `--fix`
fast-forwards the clean ones without being asked.

### Upstream pull requests

Every open pull request you have opened in a repo outside the account — the
ones you can't merge, where the only lever is the maintainer's attention. The
README celebrates these once they land; this section is the half still in
flight.

They come from one `is:pr is:open author:<owner>` search rather than a probe per
repo, because they sit where the account listing can't reach. Anything in the
account's own namespace drops out and is `prs`' to report. `--repo <name>`
narrows the search by repo name, so a run named at your fork carries the
upstream pull requests against the project it came from.

A row is what still stands between that pull request and a merge, and the
section sorts on whether any of it is yours: spending a maintainer's attention
before you have cleared every reason they could point at wastes the one lever
you have.

| Blocker | How it is read | What settles it |
| --- | --- | --- |
| head gone | the head repository answers null, so the fork is deleted | `gh pr close` — there is nothing left to merge |
| changes requested | a human's latest review, bots filtered out | read what they said |
| unresolved | review threads still open | read what they said |
| conflict | `mergeable` is `CONFLICTING` | check it out and rebase |
| behind | the base repo's own comparison of base against head | `gh pr update-branch --rebase` |
| checks | the head commit's status rollup failed | `gh pr checks` |
| draft | it is a draft, so nobody has been asked to look | `gh pr ready` |
| no reviewer | nobody reviewed it and nobody was asked | ask someone |
| waiting | nothing above is yours | nudge, with the days since anyone but you touched it |

`head gone` ends the row: a pull request whose fork is deleted has nothing left
for the rest to describe.

A pull request against an **archived** repo is left out, because an archived
repo is read-only and the API refuses every command a row could carry — `gh pr
close` among them. No flag brings it back: there is no state of the run in which
the row is worth reading. The section still names the repos it left out, since
that is the only trace of a pull request you may remember opening, and the drop
runs before the comparisons so a left-out row costs no API call.

A pull request opened from a fork of yours carries one more line, `fork behind`:
the fork's default branch measured against its parent's, with the `gh repo sync`
that closes it. Nobody is waiting on it, so it is not a blocker and does not
move the row into the ones waiting on you — but the fork's default branch is
where your next branch comes from, so it drifts quietly and charges you a rebase
later. The comparison is read once per fork, since a fork can carry several pull
requests.

Being behind is read off `compare/<base>...<head owner>:<head>` rather than off
`mergeStateStatus`, which reports `BEHIND` only where the repo requires an
up-to-date branch — the count holds either way. A review or comment from CI is
not a maintainer waiting on an answer, so `__typename: Bot` and a `[bot]` login
are both filtered out before any of this is counted.

The section writes nothing. `--fix` passes it by for the reason it passes
`maturity` by: every command here reaches a repo on GitHub rather than the clone
tree the rest of the run reconciles — a maintainer's project, or your own fork —
and belongs with a person's judgement.

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

### Where behind stops and reconcile starts

`behind` measures a clone against origin, so a clone with no origin to measure
against is `reconcile`'s finding rather than its own. Two shapes reach it: a
work tree with no `origin` remote at all, which `reconcile` names among the
clones it reports local state for, and one whose origin answers
`Repository not found`, which `reconcile` reports as cloned but no longer on
GitHub. Both leave `behind` before it counts anything; `--all-details` is where
they still show, with the reason. Any other fetch failure is the clone's own
and stays where it was found.

### What a run covers

A run lists the whole account and then holds back whatever `ignore.yml` says it
isn't looking at — forks, archived repos, and the names. A `scope:` line at the
top of the report names every held-back repo under the reason it was held and
ends with the flags that widen the run — `--include-forks`,
`--include-archived`, `--no-ignore`. Holding them back after the listing rather
than filtering them at the API is what lets the report name them, and lets
`reconcile` accept their clones without spending a call working out what they
are.

Their clones stay in the local passes. `uncommitted` and `behind` still read a
fork's clone, because work only your disk holds is worth reporting whatever the
run's remote scope is. A repo named in `ignore.yml` is the exception: its clone
drops out with it.

### Maturity and alerts

These two are the only sections that report on a project rather than on work
queued against it, and neither writes. `--fix` passes them by: closing a
maturity gap means turning on a setting or committing a file, and both belong
with a person's judgement rather than inside a read-only sweep. The `ratchet`
and `triage-alerts` skills are where that half lives.

`maturity` measures every project against `maturity.yml` and reports the tier it
has reached, the checks still open, and which of those block the next tier. A
check that a single call settles prints that call; one that needs a commit
prints nothing, which is how the page knows to mark it advisory and keep it out
of the copy-all list.

A check asks whether the thing works, not whether it is present. The one that
makes the difference is `dependabot-config`: a config naming `github-actions`
at any directory other than `/` parses, reads as configured on every dashboard,
and matches no manifest, so the check rejects it and reports why.

`alerts` reads the three security feeds and sorts by severity. A feed a repo has
turned off answers 404, which is the disabled setting `maturity` already
reports — so it counts as no alerts rather than as an error, and the same probe
feeds the `no-open-alerts` check.

### The ledger

A `maturity` run appends to `maturity-history.yml`, and only where a project's
tier or gap count actually moved — the file is a record of movement, not of
runs. The bar is versioned into the same file beside it, because a tier that got
harder to reach is what separates a project sliding back from one standing still
under a raised bar. `--no-record` holds it back, which is what a single-repo run
wants.

### Where a name takes you

Every project a section names is an OSC 8 hyperlink to the subresource that
section is about — the branch sections and `uncommitted` point at `/branches`,
`unreleased` at `/releases`, `prs` and `upstream` at `/pulls`, `issues` at
`/issues`, `behind` at the commit log of the branch it measured. The detail beside the name keeps
its own link, so a pull request row carries both the repo's PR list and that
pull request. A clone's link is built from the `owner/name` parsed out of its
`origin`, never from the directory it sits in. Where output isn't a terminal
the URL prints inline after the label instead.

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

### The page

Every run writes `output/repo-status.html` alongside the terminal report, and
`--open` opens it. The findings are already gathered by the time the report
prints, so the page costs a template rather than a flag. The page is
self-contained — no CDN, no build step, no network at view time — and shares
`repo-viz.html`'s palette and theme toggle.

The chrome is kept out of the way: hairline rules rather than cards, one column,
a line of figures rather than tiles, and the whole of it in 13px. What weight
there is goes to the commands.

Where the terminal prints a paste-ready command under each actionable finding,
the page sets that command as a code block, the heaviest element on the row, and
copies it on click. A row carrying more than one takes them all at once with
`copy N`. A command that takes you to the work rather than settling it — the
`cd` under a dirty tree — is marked `advisory` and stays out of that copy and
out of the `commands ready` figure, while staying copyable on its own.

The sections carry their run order as a numbered rail across the top,
which doubles as jump-nav and as the count at a glance. Colour is spent on one
thing: a left rule marks work only one clone holds. Groups flagged `info`
(clones filtered out of the run, clones of other owners) report context rather
than work, so they stay out of every count the page presents as a finding.

Sections and rows are both `<details>`, so folding is the browser's own —
keyboard handling and all — rather than ARIA wired by hand. A section opens when
it has work in it and stays folded when it came back clear, which makes
`Collapse all` a second reading of the same page: every section, its tally, and
nothing else — the blurb saying what a section is for sits in its body, which is
where you have the question. A search opens the sections holding matches, since
a hit inside a folded one would otherwise be invisible; clearing it hands every
section back to the state it started in. Jumping from the rail opens its target
for the same reason.

A section's heading pins under the toolbar for as long as you are inside the
section, so the control that folds it is wherever you are reading rather than
back where you entered, and the heading says which section you are in. Folding
from a pinned heading would leave you scrolled past where the section used to
end, so the heading returns to where you clicked it. Only a fold a person made
is recovered that way; `Collapse all` and the filter move nothing, on the same
`_auto` check that keeps them from rewriting what you folded.

A row you have dealt with is dismissed, and the tally, the rail, the figures at
the top and the copy list all drop it in the same pass — a page that counted a
row it no longer shows would be worth less than no count at all. `Put back`
returns them, per section or for the run.

Both foldings and the dismissals live in `sessionStorage`, keyed by owner: the
page is rewritten on every run, so a row you set aside should outlast a scroll
and a reload and nothing more. A blocked or absent store is not an error, the
page just starts fresh. Since a `<details>` raises the same event whether a
person clicked it or the filter opened it, the page records the value it set
itself and ignores the event that follows, which is what keeps a search from
rewriting what you folded.

### Adding a section

A section is three functions:

| Function | Does |
| --- | --- |
| `collect_<name>` | Takes already-gathered state, returns findings. Reads only. |
| `section_<name>` | Prints those findings, and carries out `--fix`. |
| `page_<name>` | Turns them into `(groups, clean-message)` for the page. |

Add its name to `SECTIONS`, its tally noun to `SECTION_UNITS`, its heading and
blurb to `SECTION_TITLES`, and its builder to the map in `build_payload`. Gather
whatever remote data it needs inside `probe_repo` behind a `wanted` check so
unselected sections cost no API calls, and call all three from `main` in report
order.

A section whose data isn't per-repo gets its own `probe_<name>` instead, called
from `main` under the same `wanted` check — `upstream` searches once across
GitHub rather than asking each repo. The probe is where the reading lives either
way, so `collect_*` stays pure.

The split is what keeps the two renderings honest: both read the same findings,
so a section cannot say one thing in the terminal and another on the page.
`collect_*` must stay free of side effects — it runs even under `--json`, where
nothing is printed and nothing is fixed.

The page speaks in the same three levels for every section — `group`, `item`,
`step` — so one renderer draws all  A step carries either `text` or
`cells` (labelled spans that line up across rows), and optionally the command
that settles it.

Wrap each probe in `attempt("<area>", …)` and have the section print
`r["errors"]["<area>"]` itself. A repo can answer some endpoints and 404 on
others (pull requests turned off is the common case), so the failure has to
reach the section that asked for it and leave the rest of the report alone.

## ignore.yml

`ignore.yml` names what a scan skips, so a repo you are done working in stops
costing API calls on every run. Both scripts read it from their own
directory rather than the working directory, and `--no-ignore` scans everything
anyway.

```yaml
archived: true      # skip archived repos
forks: true         # skip forks
repos:              # skipped by name, whatever their state
  - name: home-tech
    reason: personal
  - name: logbook
    reason: maintenance
  - name: moor
    reason: maintenance
```

A `repos:` entry is either a bare name or a `name:` / `reason:` mapping. The
reason is free text and decides one thing: which bucket the `scope:` line
reports the repo under, so several projects skipped for the same cause read as
a group rather than as a list of unexplained names. An entry that gives no
reason reports under the file itself, and `--no-ignore` is what widens a run
past every name whatever its reason.

Each script loads it with `yaml.safe_load` and then **rejects anything the two
keys don't cover**, down to an unrecognized key — inside a `repos:` entry as
much as at the top level: a line dropped in silence would widen a scan without
saying so, which is the one failure an ignore list cannot have. Both scripts
carry their own copy of that loader, so a change to one belongs in the other
too.

Where the two disagree, the more specific instruction wins:
`repo-status.py --repo home-tech` probes an ignored repo because you named it,
and `--include-archived` and `--include-forks` outrank `archived: true` and
`forks: true`.

The skip has to happen before the expensive work, not after. In `repo-viz.py`
that means filtering inside `gather` before `shape`, which paginates a repo's
whole commit history when one page won't hold it. In `repo-status.py` it means
dropping an ignored repo's clone alongside its remote record, so `reconcile`
doesn't spend an API call classifying a clone whose repo it just filtered out.
A held-back fork or archived repo keeps its clone but is recognized by name for
the same reason — the call is what the holdback saves.
Each script prints what it skipped.

## maturity.yml

`maturity.yml` is the bar. Tiers are cumulative and weakest first, so a project
reaches `tended` only once it also clears `baseline`, and one name says how far
along it is. Raising the bar is a single edit that re-measures every project at
once, which is the whole point of keeping it out of the script.

```yaml
tiers:
  baseline:
    - dependabot-alerts
    - description
  tended:
    - dependabot-config
  hardened:
    - active-ruleset

exempt:
  - name: <repo>
    checks: [dependabot-config]
    reason: no dependency manifests
```

The file names which checks sit in which tier and who is excused from what. The
checks themselves live in `repo-status.py`'s `CHECKS`, which owns how each one
is probed and the command that closes it — data here, behaviour there, so a new
check is a code change and a new bar is an edit.

An excused check counts as cleared, so a project excused from everything in a
tier reaches it. Every exemption needs a `reason`, which is what it reports
under: an exemption is the ratchet slipping, and it should read as a decision
rather than as a silent pass.

The loader **rejects anything the two keys don't cover** — an unknown key, an
unknown tier, a check name no longer in `CHECKS`, a check claimed by two tiers.
A check that fell out of the file in silence would lower the bar without saying
so, which is the one failure a ratchet cannot have. `ignore.yml` refuses
unrecognized input for the same reason, and the two loaders are separate.

## repo-viz.py

The same account read as attention rather than as a worklist: what you have been
pouring your weeks into, and what has gone quiet. Driven through the `gh` CLI,
with PyYAML the only import outside the standard library. It writes a
self-contained HTML page — no CDN, no build step, no network at view time.

```bash
./repo-viz.py                       # write output/repo-viz.html and open it
./repo-viz.py --months 24           # widen the window
./repo-viz.py --owner some-user     # someone else's public projects
./repo-viz.py --out /tmp/x.html --no-open
./repo-viz.py --json                # the gathered data, for another tool
```

Both scripts write their page on every run, and differ on whether it opens.
Here the page is the whole deliverable (the terminal gets one summary line), so
a run opens it and `--no-open` holds it back. `repo-status.py` defaults the
other way, since its terminal report is what you came for and its page is a
second reading of the same findings.

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
forks and private repos each toggle). What `ignore.yml` holds back never
reaches the filter row at all, so `--no-ignore` is what brings a fork or an
archived repo back, flagged `fork` or `archived` on the tile and in the table —
and the Forks toggle is what sorts them once they are there:

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
