# AGENTS.md

## What this repo is

`chris-peterson/chris-peterson` is a GitHub profile repo

| Path | What it is |
| --- | --- |
| `README.md` | Rendered on the GitHub profile page. Points at the docs site. |
| `repo-status.py` | A cross-repo dashboard and reconciler for every project under the account |

## repo-status.py

One script, standard library only, driven entirely through the `gh` CLI and
`git`. Run it from anywhere; it defaults to the authenticated user and a clone
tree at `~/src/github/<owner>`.

```bash
./repo-status.py                          # all seven sections, read-only
./repo-status.py --only unreleased        # one section
./repo-status.py --skip issues --skip prs
./repo-status.py --fix                    # reconcile, prompting before each delete
```

Sections print in a fixed order, each one independently selectable with
`--only` / `--skip`: `reconcile`, `freshness`, `local-branches`,
`orphan-branches`, `unreleased`, `prs`, `issues`.

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
