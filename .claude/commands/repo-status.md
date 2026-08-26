---
description: Cross-repo dashboard for clones out of sync, stale branches, unreleased commits, open PRs and issues
argument-hint: "[section] [repo] [--fix]"
disable-model-invocation: true
---

Run `./repo-status.py` from the repo root with the flags below, then report what it found. Surface the findings and the paste-ready commands the report already prints; add no preamble, no restatement of what the script does, and no follow-up work.

## Turning the arguments into flags

`$ARGUMENTS` is free-form. Anything that already looks like a flag passes through verbatim. Otherwise:

| What was typed | Flag |
| --- | --- |
| a section name (`reconcile`, `freshness`, `local-branches`, `orphan-branches`, `unreleased`, `prs`, `issues`) | `--only <section>`, repeatable |
| `no <section>`, `skip <section>` | `--skip <section>`, repeatable |
| a bare repo name | `--repo <name>`, repeatable |
| nothing | no flags: all seven sections, read-only |

A prefix that unambiguously names one section (`orphan`, `unreleased`, `prs`) resolves to it. One that matches several, like `branches`, is a question rather than a guess.

## What to report

The script's own output is the deliverable. Lead with the sections that have findings, drop the ones that came back clean, and keep the commands it printed verbatim in a fenced block so they can be pasted.

Each section prints its own probe errors. A repo can answer some endpoints and 404 on others (pull requests turned off is the common case), so an error under `prs` says nothing about the rest of the report. Report it where it appeared and leave the other sections' findings standing.

## --fix

`--fix` writes. It clones what is missing and fast-forwards what is cleanly behind without asking, then asks y/n before anything that moves or removes.

Those prompts need a terminal. Run from here, stdin isn't a tty, so every move and delete prints `skipped, needs a terminal to confirm` and the run does only the safe half. When the arguments ask for `--fix`, say that and hand back the line to run in the shell instead:

```
! ./repo-status.py --fix
```

Never add `--yes` unless it was typed. It is what turns those prompts off.
