# Overview

👋 Hi, I'm Chris.  I've been writing software for the last couple of decades.

Lately, I've been working on <a href="https://chris-peterson.github.io/pwsh-gitlab"><img src="https://chris-peterson.github.io/pwsh-gitlab/icon.png?raw=true" width=32 />`pwsh-gitlab`</a>, a PowerShell wrapper around [GitLab API](https://docs.gitlab.com/ee/api/).

The `GitlabCli` module is used to perform tasks that would otherwise be difficult/tedious (e.g. [cloning all the projects in a group](https://github.com/chris-peterson/pwsh-gitlab#clone-gitlabgroup-aka-copy-gitlabgrouptolocalfilesystem)).
My favorite part of using this module is that it has _implicit context_ based on your working directory making it easy to jump back and forth between
local code artifacts and remote API resources.  You can invoke APIs, or simply pipe any object to `| go` to navigate to the Web UI, e.g. `mr | go`
brings you to the [Merge Request UI](https://docs.gitlab.com/ee/user/project/merge_requests/) for your current branch.

Give it a try [here](https://github.com/chris-peterson/pwsh-gitlab#getting-started).

## Active Projects
* [Assurance](https://github.com/chris-peterson/assurance#overview) - a library to boost confidence when making code changes.
* [Kekiri](https://github.com/chris-peterson/kekiri#overview) - a testing framework for writing low-ceremony BDD tests using Gherkin language.
* [Spiffy](https://github.com/chris-peterson/spiffy/#overview) - a battle-tested observability framework for logs and metrics.

# Other

When I'm not writing code, I try my best to [be a good human](https://github.com/chris-peterson/project-human?tab=readme-ov-file#overview)
