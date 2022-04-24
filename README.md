# Overview

👋 Hi, I'm Chris.  I've been writing software for the last few decades.

Lately, I've been working on:

## [pwsh-gitlab](https://github.com/chris-peterson/pwsh-gitlab)

a PowerShell wrapper around [GitLab API](https://docs.gitlab.com/ee/api/)

I use the `GitlabCli` module for performing various tasks that would otherwise be difficult/tedious (e.g. [cloning all the projects in a group](https://github.com/chris-peterson/pwsh-gitlab#clone-gitlabgroup-aka-copy-gitlabgrouptolocalfilesystem)).
My favorite part of using this module is that it has _implicit context_ based on your working directory making it easy to jump back and forth between
local code artifacts and remote API resources.  You can invoke APIs, or simply pipe any object to `| go` to navigate to the Web UI, e.g. `mr | go`
brings you to the [Merge Request UI](https://docs.gitlab.com/ee/user/project/merge_requests/) for your current branch.

Give it a try [here](https://github.com/chris-peterson/pwsh-gitlab#getting-started)

## Other Projects

I've created and actively maintain a few projects, including

### [Kekiri](https://github.com/chris-peterson/kekiri#overview)

### [Spiffy](https://github.com/chris-peterson/spiffy/#overview)

#### Contributions to OSS

I've made a few (small) contributions to various projects that I've used throughtout the years, including:
* [Newtownsoft.Json](https://github.com/JamesNK/Newtonsoft.Json)
* [Swashbuckle](https://github.com/domaindrivendev/Swashbuckle.WebApi)
* [Autofac](https://github.com/autofac/Autofac)
* [NHibernate](https://github.com/nhibernate/nhibernate-core)
* and more :)
