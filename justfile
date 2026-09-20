# What this is, and every recipe there is
[private]
default:
    @echo ""
    @echo "  A cross-repo dashboard for every project under a GitHub account:"
    @echo "  what is unfinished, what is unreleased, and where the weeks went."
    @echo ""
    @echo "  New here?   just demo     one project, narrated end to end"
    @echo "  Broken?     just check    verify what every run needs"
    @echo ""
    @just --list --unsorted --list-prefix '    ' --list-heading ''
    @echo ""
    @echo "  Every recipe reads GitHub through the gh CLI. Only 'just fix' writes."
    @echo "  Any recipe takes the script's own flags: just status --all-details"
    @echo ""

# Verify what every run needs, and touch nothing
[group('start here')]
check:
    #!/usr/bin/env bash
    set -uo pipefail
    fail=0
    ok()   { printf '  ok    %s\n' "$1"; }
    miss() { printf '  MISS  %s\n' "$1"; fail=1; }
    echo
    if command -v python3 >/dev/null; then ok "python3 $(python3 -V | cut -d' ' -f2)"
    else miss "python3: install it"; fi
    if python3 -c 'import yaml' 2>/dev/null; then ok "PyYAML"
    else miss "PyYAML: run 'just install'"; fi
    if command -v gh >/dev/null; then ok "gh $(gh --version | head -1 | cut -d' ' -f3)"
    else miss "gh: install the GitHub CLI"; fi
    owner=$(gh api user --jq .login 2>/dev/null || true)
    if [ -n "$owner" ]; then ok "gh authenticated as $owner"
    else miss "gh auth: run 'gh auth login'"; fi
    root="$HOME/src/github/${owner:-?}"
    if [ -n "$owner" ] && [ -d "$root" ]; then ok "clone tree $root"
    elif [ -n "$owner" ]; then miss "clone tree $root: 'just fix' clones what is missing"; fi
    if python3 - <<'PY'
    import importlib.util as util, sys
    for name, path, loaders in (("rs", "repo-status.py", ("load_ignore", "load_maturity")),
                                ("rv", "repo-viz.py", ("load_ignore",))):
        spec = util.spec_from_file_location(name, path)
        mod = util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for loader in loaders:
            try:
                getattr(mod, loader)()
            except Exception as err:
                sys.exit(f"        {path}: {err}")
    PY
    then ok "ignore.yml and maturity.yml parse, under both scripts' loaders"
    else miss "a config file was rejected; the loader printed why, above"; fi
    echo
    exit $fail

# One project, narrated end to end, for someone who has never seen this
[group('start here')]
demo repo='chris-peterson':
    #!/usr/bin/env bash
    set -euo pipefail
    echo
    echo "==> just check"
    echo "    every run needs gh, its auth, and two config files that parse"
    just check
    echo "==> ./repo-status.py --repo {{repo}} --no-record"
    echo "    one project rather than the whole account, and the tier ledger left alone"
    echo
    ./repo-status.py --repo {{repo}} --no-record
    echo
    echo "==> the same findings as a page: output/repo-status.html"
    echo "    'just status' covers the account, 'just viz' charts where the weeks went,"
    echo "    'just open' opens both pages."

# Install the one dependency either script has
[group('start here')]
install:
    python3 -m pip install -r requirements.txt

# Every section, read-only, for the whole account
[group('what is on the board')]
status *args:
    ./repo-status.py {{args}}

# One project, leaving the tier ledger alone: just repo moor
[group('what is on the board')]
repo name *args: (status '--repo' name '--no-record' args)

# One section: just section unreleased
[group('what is on the board')]
section name *args: (status '--only' name args)

# The section names 'just section' takes, in the order a run prints them
[group('what is on the board')]
sections:
    @python3 -c "import importlib.util as u; s = u.spec_from_file_location('rs', 'repo-status.py'); m = u.module_from_spec(s); s.loader.exec_module(m); print('\n'.join(m.SECTIONS))"

# The same findings as JSON, for another tool
[group('what is on the board')]
status-json *args: (status '--json' args)

# Where attention went, week by week: just viz 24
[group('what is on the board')]
viz months='12' *args:
    ./repo-viz.py --months {{months}} {{args}}

# Reconcile the clone tree and the branches, asking before each move or deletion
[group('act on it')]
fix *args: (status '--fix' args)

# Open the pages the last run wrote
[group('the pages')]
open:
    #!/usr/bin/env bash
    set -euo pipefail
    found=0
    for page in output/repo-status.html output/repo-viz.html; do
        if [ -f "$page" ]; then open "$page"; found=1; fi
    done
    [ "$found" = 1 ] || echo "no pages yet: run 'just status' or 'just viz'"

# Delete the pages and the bytecode cache
[group('the pages')]
clean:
    rm -rf output __pycache__
