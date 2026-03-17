# pwsh-gitlab

A PowerShell wrapper around the [GitLab API](https://docs.gitlab.com/ee/api/).

The `GitlabCli` module is used to perform tasks that would otherwise be difficult or tedious, such as [cloning all the projects in a group](https://github.com/chris-peterson/pwsh-gitlab#clone-gitlabgroup-aka-copy-gitlabgrouptolocalfilesystem).

## Highlights

- **Implicit context** based on your working directory — easily jump between local code and remote API resources
- Invoke APIs directly, or pipe any object to `| go` to navigate to the Web UI
- Example: `mr | go` opens the [Merge Request UI](https://docs.gitlab.com/ee/user/project/merge_requests/) for your current branch

## Get Started

Check out the [full documentation](https://chris-peterson.github.io/pwsh-gitlab) for installation instructions and usage guides.

> [Quick Start Guide](https://chris-peterson.github.io/pwsh-gitlab/#/?id=quick-start)

## Links

- [Documentation (Wiki)](https://chris-peterson.github.io/pwsh-gitlab)
- [GitHub Repository](https://github.com/chris-peterson/pwsh-gitlab)
