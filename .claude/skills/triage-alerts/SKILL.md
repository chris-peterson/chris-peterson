---
name: triage-alerts
description: This skill should be used when the user asks to "triage alerts", "triage the security alerts", "address the security findings", "clear the dependabot alerts", "fix the code scanning alerts", "deal with the secret scanning alert", or when repo-status.py has reported open security alerts. Works each open Dependabot, code scanning and secret scanning alert to a fix or a reasoned dismissal.
---

# Triage alerts

The `alerts` section lists what the three feeds have found. This works them.
Alerts differ from the maturity gaps in one way that decides everything here:
a gap is the same everywhere and can be closed in bulk, while an alert is a
claim about one repo's code that has to be read before it is acted on.

## Read what is open

```bash
./repo-status.py --only alerts --json
```

Each repo's `alerts` block holds the three feeds separately. Take them most
severe first — the report already sorts that way, and a critical Dependabot
alert on a published package outranks a `note`-level code scanning finding in a
script nobody runs.

## Dependabot alerts

A vulnerable dependency, with a fixed version named in the advisory. Two paths:

- **Security updates are on** — Dependabot opens the PR itself. The alert
  closes when that PR merges, so the work is reviewing and merging it, not
  bumping by hand. Check for an open one before touching the manifest:
  `gh pr list -R <repo> --author app/dependabot`.
- **Nothing opened** — bump the manifest to the fixed version yourself, on a
  branch, through `/anchor:commit`.

Where the advisory doesn't reach the project — a dev-only dependency, a code
path that isn't built — dismiss it with the reason rather than leaving it open:

```bash
gh api -X PATCH repos/<repo>/dependabot/alerts/<number> \
  -f state=dismissed -f dismissed_reason=not_used
```

`dismissed_reason` takes `fix_started`, `inaccurate`, `no_bandwidth`,
`not_used`, or `tolerable_risk`. Pick the one that is true; the field is what a
future reader sees instead of the alert.

## Code scanning alerts

CodeQL's default setup on a repo of workflows reports mostly on the workflows
themselves. The common one across this account is **"Workflow does not contain
permissions"** — a job running with the token's default scope instead of a
declared one. The fix is a `permissions:` block naming what the job actually
needs, at the workflow's top level or on the job:

```yaml
permissions:
  contents: read
```

Read what the job does before writing the block. A job that pushes a projection
back to the branch needs `contents: write`; one that only checks out and tests
needs `contents: read`. Getting this wrong breaks the workflow, which is worse
than the finding.

Dismiss through the same endpoint shape when a finding genuinely doesn't apply:

```bash
gh api -X PATCH repos/<repo>/code-scanning/alerts/<number> \
  -f state=dismissed -f dismissed_reason=false\ positive
```

## Secret scanning alerts

**Treat every one of these as live until proven otherwise**, and never paste the
secret into the conversation, a commit, or a file. The order matters: rotate the
credential at its source first, then remove it from the code, then close the
alert. Removing it from the tree first leaves a working secret in the history
with nobody watching it.

Revoking the credential is the user's to do — it needs access this session
doesn't have and shouldn't. Say which credential, where it surfaced, and what
revoking it will break, then wait.

## Report what was done

Close by saying, per alert: fixed and where, dismissed and why, or left open and
what it is waiting on. An alert that was neither fixed nor dismissed is still
counted by the next run, which is the intended behaviour — say so rather than
implying the feed is clear.
