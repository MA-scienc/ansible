"""Generate an offline pull request impact map from two Git revisions."""
import argparse
import ast
from collections import defaultdict
from html import escape
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess


def git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout


def snapshot(repo, commit):
    entries = {}
    for record in git(repo, "ls-tree", "-rz", commit).split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, _, oid = meta.split()
        path = name.decode("utf-8", "surrogateescape")
        if mode.startswith(b"100") and path.lower().endswith((".py", ".ps1")):
            entries[path] = oid
    if not entries:
        return {}
    source = io.BytesIO(git(repo, "cat-file", "--batch",
                            data=b"\n".join(entries.values()) + b"\n"))
    files = {}
    for path in entries:
        _, _, size = source.readline().split()
        files[path] = source.read(int(size))
        source.read(1)
    return files


# Deliberately narrow: standalone literal script invocations and script-root paths.
PYTHON_CALL = re.compile(
    r"""^\s*(?:&\s+)?(?:python(?:3|\.exe)?|py(?:\.exe)?)\s+"""
    r"""(?:"([^"]+\.py)"|'([^']+\.py)'|([^\s;|]+\.py))(?=\s|$|[;|])""",
    re.IGNORECASE,
)


def relationships(files, roots, powershell_workdir):
    modules = defaultdict(set)
    locations = defaultdict(set)
    for path in files:
        if not path.endswith(".py"):
            continue
        for root in roots:
            try:
                relative = PurePosixPath(path).relative_to(root)
            except ValueError:
                continue
            parts = relative.with_suffix("").parts
            package = parts[:-1]
            module = package if parts[-1] == "__init__" else parts
            if module:
                modules[module].add(path)
                locations[path].add(package)

    edges = set()
    unresolved = set()

    def resolve(source, line, names):
        matches = set()
        for name in names:
            if len(modules.get(name, ())) == 1:
                matches.update(modules[name])
        edges.update((source, target, line, "imports") for target in matches
                     if target != source)

    for path, data in files.items():
        if path.endswith(".py"):
            if path not in locations:
                continue
            try:
                tree = ast.parse(data, filename=path)
            except (SyntaxError, ValueError):
                unresolved.add((path, 0, "Python parse failed"))
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    resolve(path, node.lineno, [tuple(a.name.split(".")) for a in node.names])
                elif isinstance(node, ast.ImportFrom):
                    suffix = tuple(node.module.split(".")) if node.module else ()
                    for package in locations[path] if node.level else [()]:
                        if node.level > len(package):
                            continue
                        prefix = package[:len(package) - node.level + 1] if node.level else ()
                        base = prefix + suffix
                        resolve(path, node.lineno, [base] + [
                            base + (a.name,) for a in node.names if a.name != "*"
                        ])
        else:
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            block_comment = False
            here_string_end = None
            for line, value in enumerate(data.decode(encoding, "replace").splitlines(), 1):
                stripped = value.strip()
                if here_string_end:
                    if stripped.startswith(here_string_end):
                        here_string_end = None
                    continue
                if block_comment or stripped.startswith("<#"):
                    block_comment = "#>" not in stripped
                    continue
                if stripped.endswith(('@"', "@'")) and not stripped.startswith("#"):
                    here_string_end = stripped[-1] + "@"
                    continue
                match = PYTHON_CALL.match(value)
                if not match:
                    if re.match(r"^\s*(?:&\s+)?(?:python(?:3|\.exe)?|py(?:\.exe)?)\s+", value, re.I):
                        unresolved.add((path, line, "Unsupported Python invocation"))
                    continue
                target = next(part for part in match.groups() if part is not None)
                target = target.replace("\\", "/")
                candidate = None
                if match.group(2) is None and target.lower().startswith("$psscriptroot/"):
                    relative = target.split("/", 1)[1]
                    if "$" not in relative and "`" not in relative:
                        candidate = PurePosixPath(path).parent / relative
                elif powershell_workdir is not None and target.startswith(("./", "../")):
                    if "$" not in target and "`" not in target:
                        candidate = powershell_workdir / target
                if candidate is not None:
                    normalized = posixpath.normpath(candidate)
                    if normalized in files:
                        edges.add((path, normalized, line, "launches"))
                        continue
                unresolved.add((path, line, "Python script path needs a known working directory or script root"))
    return edges, unresolved


def render(repo, base, head, changed, edges, notes, output, limit):
    def h(value):
        return escape(value.encode("utf-8", "replace").decode())
    nodes = set(changed)
    for source, target, _, _, _ in edges:
        nodes.update((source, target))
    ordered = sorted(nodes, key=lambda path: (path not in changed, path))
    ids = {path: f"file-{i}" for i, path in enumerate(ordered)}
    shown = set(ordered[:limit])
    colors = {"A": "#b7edcd", "D": "#f8c4c4", "M": "#ffe1a1"}
    lines = ["digraph G { rankdir=LR; node [shape=box,style=filled,fontname=\"sans-serif\"];"]
    pairs = set()
    for path in ordered[:limit]:
        color = colors.get(changed.get(path, ""), "#dce8fa")
        label = json.dumps(path.encode("utf-8", "replace").decode(), ensure_ascii=False)
        lines.append(f'"{ids[path]}" [label={label},fillcolor="{color}",URL="#{ids[path]}"];')
    for source, target, line, kind, revision in edges:
        pair = (source, target, kind, revision)
        if source in shown and target in shown and pair not in pairs:
            pairs.add(pair)
            style = "dashed" if revision == "base only" else "solid"
            lines.append(f'"{ids[source]}" -> "{ids[target]}" [label="{kind}",style={style}];')
    lines.append("}")
    svg = subprocess.run(["dot", "-Tsvg"], input="\n".join(lines), text=True,
                         stdout=subprocess.PIPE, check=True).stdout
    svg = svg[svg.index("<svg"):]
    evidence = defaultdict(list)
    for source, target, line, kind, revision in edges:
        evidence[source].append(f"{source}:{line} {kind} {target} ({revision})")
        if target != source:
            evidence[target].append(f"{source}:{line} {kind} {target} ({revision})")
    sections = []
    for path in ordered:
        status = changed.get(path, "Related file")
        items = "".join(f"<li>{h(item)}</li>" for item in evidence[path])
        diff = ""
        if path in changed:
            patch = git(repo, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv",
                        base, head, "--", path).decode("utf-8", "replace")
            diff = f"<details><summary>Git diff</summary><pre>{h(patch)}</pre></details>"
        sections.append(f'<section id="{ids[path]}"><h2>{h(path)} · {h(status)}</h2><ul>{items}</ul>{diff}</section>')
    warnings = "".join(f"<li>{h(path)}:{line} — {h(reason)}</li>"
                       for path, line, reason in sorted(notes))
    output.write_text(f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Pull request impact map</title>
<style>
body{{font:16px system-ui;margin:2rem;color:#162438;background:#f7f9fc}}
.graph,pre{{overflow:auto;background:white;padding:1rem;border:1px solid #d5dce8}}
section{{padding:1rem 0;border-bottom:1px solid #d5dce8}}section:target{{background:#eef4ff}}
h2{{font-size:1.1rem;overflow-wrap:anywhere}}
</style>
<h1>Pull request impact map</h1>
<p>{base[:12]} → {head[:12]} · {len(changed)} changed files · {len(nodes)} total files in report</p>
<p>Green added · Amber modified · Red deleted · Blue unchanged neighbor.<br>
Arrows point from the referencing file to its dependency. Dashed arrows existed only in the base revision.</p>
<p>Showing {len(shown)} of {len(nodes)} files in the diagram. File details below include all matching relationships.</p>
<div class="graph">{svg}</div>
<details><summary>Analysis diagnostics ({len(notes)})</summary><ul>{warnings}</ul></details>
{''.join(sections)}
""", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("base", help="Target branch or base commit; merge base is used")
    parser.add_argument("head", nargs="?", default="HEAD")
    parser.add_argument("--python-root", action="append", type=PurePosixPath)
    parser.add_argument("--powershell-working-dir", type=PurePosixPath)
    parser.add_argument("--output", type=Path, default=Path("impact.html"))
    parser.add_argument("--max-nodes", type=int, default=60)
    args = parser.parse_args()
    if args.max_nodes < 1:
        parser.error("--max-nodes must be positive")
    try:
        head = git(args.repo, "rev-parse", "--verify", "--end-of-options", args.head + "^{commit}").decode().strip()
        target = git(args.repo, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}").decode().strip()
        base = git(args.repo, "merge-base", target, head).decode().strip()
        raw = git(args.repo, "diff", "--name-status", "--no-renames", "-z", base, head)
        tokens = raw.rstrip(b"\0").split(b"\0") if raw else []
        changed = {name.decode("utf-8", "surrogateescape"): status.decode()
                   for status, name in zip(tokens[::2], tokens[1::2])}
        roots = args.python_root or [PurePosixPath(".")]
        old, old_notes = relationships(snapshot(args.repo, base), roots, args.powershell_working_dir)
        new, new_notes = relationships(snapshot(args.repo, head), roots, args.powershell_working_dir)
        old_keys = {(a, b, kind) for a, b, _, kind in old}
        new_keys = {(a, b, kind) for a, b, _, kind in new}
        edges = [(a, b, line, kind, "head" if (a, b, kind) in old_keys else "head only")
                 for a, b, line, kind in new if a in changed or b in changed]
        edges.extend((a, b, line, kind, "base only") for a, b, line, kind in old
                     if (a in changed or b in changed) and (a, b, kind) not in new_keys)
        render(args.repo, base, head, changed, sorted(edges), old_notes | new_notes,
               args.output, args.max_nodes)
        print(f"{len(changed)} changed files, {len(edges)} matching references. {args.output}")
    except subprocess.CalledProcessError as error:
        parser.exit(1, (error.stderr or b"Command failed").decode("utf-8", "replace") + "\n")
    except OSError as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
