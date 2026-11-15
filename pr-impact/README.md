# Pull request impact map

This local CLI compares a proposed Git revision with its merge base against a target
revision. It reads tracked source as text and writes an offline HTML report. It
requires Python 3.10+, Git and Graphviz (`dot` on PATH). It reads commits and calls
local tools without running repository code or contacting a service.

## Reproduce the Ansible demo

From the workspace containing this directory and the Ansible checkout:

```bash
python3 pr-impact/impact.py ansible-demo 2bf7ee5^ 2bf7ee5 --python-root lib --output ansible-impact.html
```

Open the HTML in a browser. Select a node to jump to the file's source-line
references and, for changed files, expand its diff. Arrows point from the
referencing file to the dependency. Dashed arrows existed only in the base snapshot.
The diagram is capped at 60 files by default. File details contain the full set
of direct relationships involving changed files.

## Use it on another repository

```bash
python3 impact.py /path/to/repo origin/main HEAD --python-root src --output impact.html
```

Use `--python-root` more than once for independent source roots. Omit it if package
names are relative to the repo root. Use the Python version that can parse the
syntax used in your repository. For PowerShell, a literal invocation such as
`python "$PSScriptRoot/jobs/geocode.py"` can link to a tracked Python script.
Add `--powershell-working-dir .` if the scripts run from repo root and use literal
paths such as `python ./jobs/geocode.py`.

The map covers static Python imports and a narrow set of direct PowerShell Python
invocations. Dynamic imports, shell wrappers, PowerShell module dependencies,
runtime calls and ambiguous module names require additional analysis. A link
indicates a source reference and potential impact. Renames appear as a deletion
and addition.

For CI, run the same command with the PR target and head SHAs and sufficient Git
history to find their merge base. Use a trusted copy of this tool and publish the
report as an internal artifact with the same access controls as the repo. The
HTML embeds source diffs. PR event wiring and posting a link depend on your Git
platform.
