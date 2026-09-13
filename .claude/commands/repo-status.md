---
description: Cross-repo dashboard for clones out of sync, stale branches, unreleased commits, open PRs and issues
argument-hint: "[section] [repo] [--fix] [--open]"
disable-model-invocation: true
---

Run `./repo-status.py` from the repo root with the flags below, then report what it found. Surface the findings and the paste-ready commands the report already prints; add no preamble, no restatement of what the script does, and no follow-up work.

## Turning the arguments into flags

`$ARGUMENTS` is free-form. Anything that already looks like a flag passes through verbatim. Otherwise:

| What was typed | Flag |
| --- | --- |
| a section name (`reconcile`, `uncommitted`, `local-branches`, `orphan-branches`, `prs`, `unreleased`, `issues`, `alerts`, `maturity`, `behind`) | `--only <section>`, repeatable |
| `no <section>`, `skip <section>` | `--skip <section>`, repeatable |
| a bare repo name | `--repo <name>`, repeatable |
| `page`, `html`, `chart`, `open it` | `--open` |
| nothing | no flags: all ten sections, read-only |

A prefix that unambiguously names one section (`orphan`, `recon`, `mat`) resolves to it. One that matches several, like `branches` or `un`, is a question rather than a guess.

## What to report

The script's own output is the deliverable. Lead with the sections that have findings, drop the ones that came back clean, and keep the commands it printed verbatim in a fenced block so they can be pasted.

Each section prints its own probe errors. A repo can answer some endpoints and 404 on others (pull requests turned off is the common case), so an error under `prs` says nothing about the rest of the report. Report it where it appeared and leave the other sections' findings standing.

## The page

Every run writes `output/repo-status.html` on top of the terminal report, and prints where it landed. `output/` is gitignored, so the default path is already the right one; pass `--out` only when a different one was asked for. `--open` opens it in a browser, which only helps when the run is in front of the user.

The page carries the same findings the report does, each actionable one with the command that settles it, copied on click. Report the findings as above, and add where the page landed when the arguments asked for it.

## Maturity and alerts

`maturity` measures every project against the bar in `maturity.yml` and prints
what each one needs to reach its next tier; `alerts` lists what the three
security feeds have found. Neither writes to a repo — `--fix` does not touch
them. Closing a maturity gap is the `ratchet` skill's job, and working an alert
is `triage-alerts`; name the skill when reporting rather than running the
commands here.

A `maturity` run appends to `maturity-history.yml` wherever a project's tier or
gap count moved, and prints those moves under `since the last recorded run`. A
regression prints red. `--no-record` holds the ledger back, which is what a
single-repo run wants — the file is meant to hold whole-account sweeps.

## --fix

`--fix` writes. It clones what is missing and fast-forwards what is cleanly behind without asking, then asks y/n before anything that moves or removes.

Those prompts need a terminal. Run from here, stdin isn't a tty, so every move and delete prints `skipped, needs a terminal to confirm` and the run does only the safe half. When the arguments ask for `--fix`, say that and hand back the line to run in the shell instead:

```
! ./repo-status.py --fix
```

Never add `--yes` unless it was typed. It is what turns those prompts off.
