---
paths:
  - README.md
---

The profile is raw HTML tables of shields.io badges, in the Dracula palette:
`282A36` and `343746` for the badge bodies, an accent for the label or logo.

| Table | A row is |
| --- | --- |
| Current Focus | a bridge.ai plugin, described in the tagline the plugin uses for itself |
| Featured Projects | a project badge to its docs site, a stars badge, a one-line description |
| Other Projects | a project badge and a one-line description |
| Upstream Contributions | one badge per upstream repo, sorted by `<owner>/<repo>` ignoring case |

An upstream badge takes one of two shapes, by how the work landed:

- **Merged pull requests**: an `issues-search` badge counting
  `repo:<owner>/<repo> is:pr author:chris-peterson is:merged`, linked to that
  same search on the repo's pull requests.
- **A commit a maintainer carried in**: a static `badge`, linked to the commit,
  with `landed as commit <sha>, carried in by a maintainer` in its title.

The `title` says what the change did for someone using that project, not what
the diff touched. The `logo` is the project's language or tool.
