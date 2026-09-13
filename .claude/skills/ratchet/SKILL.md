---
name: ratchet
description: This skill should be used when the user asks to "ratchet", "raise the bar", "raise a repo to the next tier", "enable the security settings", "turn on dependabot", "add a dependabot config", "close the maturity gaps", or when repo-status.py has reported a project below the bar in maturity.yml. Closes the gaps the maturity section measures, by API call where one settles it and by commit where it does not.
---

# Ratchet

`repo-status.py` measures; this closes what it found. The split is deliberate:
the report never writes, so a run costs nothing and can be trusted at a glance,
and every change to a repo passes through here where it can be confirmed.

## Read the gaps first

```bash
./repo-status.py --only maturity --no-record --repo <name> --json
```

`--json` skips the report and the ledger, and puts each repo's `maturity` block
where it can be read. `--no-record` keeps a single-repo run out of
`maturity-history.yml`, which is meant to hold whole-account sweeps.

Work the checks in tier order and stop at the end of the target tier. A repo
reaches a tier by clearing every check in it *and* every earlier one, so
skipping ahead to the interesting check leaves the tier unreached and the
ledger unmoved.

## Gaps a single call closes

The report prints the command for each of these. Confirm the whole set with the
user, then run them one per `Bash` call — they write to a live repo, and a
failure halfway through a chain leaves it unclear which half landed.

| Check | What it turns on |
| --- | --- |
| `dependabot-alerts` | `PUT /repos/{repo}/vulnerability-alerts` |
| `security-updates` | `PUT /repos/{repo}/automated-security-fixes` |
| `private-reporting` | `PUT /repos/{repo}/private-vulnerability-reporting` |
| `secret-scanning` | `PATCH /repos/{repo}` → `security_and_analysis` |
| `push-protection` | same |
| `code-scanning` | `PATCH /repos/{repo}/code-scanning/default-setup` |

`description`, `topics` and `homepage` have commands too, but each carries a
`<placeholder>` the report can't fill. Read the repo's README and its docs site
before proposing values, and show them to the user as text rather than pasting
a guess into a live repo.

## Gaps that need a commit

These have no command in the report, because settling them means changing the
tree. Each lands as an ordinary change on a branch: `/anchor:commit` for the
commit, `/anchor:prepare-review` for the PR.

### `dependabot-config`

Detect the ecosystems from what is actually in the tree, and write one entry per
ecosystem found:

| Found in the tree | `package-ecosystem` |
| --- | --- |
| `.github/workflows/*.yml`, `action.yml` | `github-actions` |
| `package.json` | `npm` |
| `*.csproj`, `*.sln`, `packages.config` | `nuget` |
| `requirements.txt`, `pyproject.toml`, `Pipfile` | `pip` |
| `go.mod` | `gomod` |
| `Cargo.toml` | `cargo` |
| `Gemfile` | `bundler` |
| `Dockerfile` | `docker` |

A PowerShell module has no ecosystem of its own — the PowerShell Gallery isn't
one Dependabot supports — so a pwsh repo takes the `github-actions` entry alone.

**`github-actions` takes `directory: "/"`.** Dependabot reaches
`.github/workflows` from the root and from nowhere else, so a config rooted at
`.github` parses, looks configured on every dashboard, and matches no manifest.
The maturity check rejects it for exactly this reason and says so in the gap.

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      actions:
        patterns: ["*"]
```

The `groups` block is what keeps a weekly sweep to one pull request instead of
one per action. Add a second `updates` entry, at the directory holding its
manifests, for each other ecosystem the tree turned up.

### `license`

Ask which license before writing one — it is a legal choice, not a default.
Where the user has no preference, MIT is what the rest of the account uses. Take
the text from `gh api /licenses/<key> --jq .body`, fill in the year and the
holder, and commit it as `LICENSE`.

### `pr-checks`

The gap is that nothing runs on a pull request, so a broken change can land
unseen. What to add depends on what the project has: a test suite wants a
workflow that runs it, a generated-artifact project wants the projection job
that proves the committed output matches its source. Read the existing
workflows and propose one that fits — never drop in a generic template.

### `active-ruleset`

A ruleset that exists but sits at `enforcement: disabled` protects nothing, and
one whose conditions name no ref targets nothing even when it is on. Read the
current ruleset, change only what is wrong, and send the whole object back:

```bash
gh api repos/{repo}/rulesets/<id> > "$(mktemp -u /tmp/ruleset.XXXXXX).json"
```

Edit that file, then `PUT` it with `--input <path>`. A partial body is not worth
guessing at — the endpoint replaces the ruleset.

## When a check should be excused instead

Not every gap is worth closing. A repo with no dependency manifests has nothing
for `dependabot-config` to watch; a scratch repo may never want a homepage. That
is a real answer, and it belongs in `maturity.yml` rather than in a repo left
permanently below the bar:

```yaml
exempt:
  - name: <repo>
    checks: [dependabot-config]
    reason: no dependency manifests
```

An excused check counts as cleared, so the repo can still reach its tier. Ask
before adding one — an exemption is the ratchet slipping, and it should be a
decision rather than a way around a gap.

## Close the loop

Re-run the measurement once the changes are in, so the ledger records the move:

```bash
./repo-status.py --only maturity
```

The run prints what changed since the last recorded one, and appends to
`maturity-history.yml` only where a tier or a gap count actually moved.
