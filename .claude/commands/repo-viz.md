---
description: Chart where attention went across the account's projects, week by week, as a self-contained HTML page
argument-hint: "[months] [owner] [--json]"
disable-model-invocation: true
---

Run `./repo-viz.py` from the repo root with the flags below, then report the one-line summary it prints on stderr: the commit count, the project count, and where the page landed. It opens the page itself, so there is nothing further to do.

## Turning the arguments into flags

`$ARGUMENTS` is free-form. Anything that already looks like a flag passes through verbatim. Otherwise:

| What was typed | Flag |
| --- | --- |
| a bare number, or `18 months`, `2 years` | `--months <n>` |
| a GitHub account name | `--owner <name>` |
| nothing | no flags: the authenticated user, 12 months, written to `repo-viz.html` and opened |

`repo-viz.html` is gitignored, so the default output path is already the right one. Pass `--out` only when a different path was asked for.

## --json

`--json` writes the gathered data to stdout instead of building the page. It is the input for another tool, so with `--json` redirect it to a file and report the path rather than printing the payload into the conversation:

```bash
./repo-viz.py --json > "$(mktemp -u /tmp/repo-viz.XXXXXX).json"
```

## Reading the page

Five views share one filter row: window length, and forks, archived and private repos each toggle. **focus** is weekly commits as a stacked area. **map** is every project against every week, sorted so attention reads as a diagonal. **rhythm** is a weekday × hour punch card in local time. **treemap** is repo size filled by commits and grouped by language. **table** is every number the charts show.

Only the owner's own commits count, since every history query is filtered by their user node id and a fork tracking upstream would otherwise bury the work. Each repo also carries an unfiltered `everyone` count, which the tooltips use to say how much of the branch is someone else's.
