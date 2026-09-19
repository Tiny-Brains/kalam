#!/usr/bin/env python3
"""Stage a definition set with this deployment's connector settings, and name the result.

    stage_set.py <src-dir> <stage-dir> [--drop=<path> ...] [connector=key=value ...]   # prints the version

Copies the set, writes each setting into the staged connector, and prints a version derived from
the staged content. `orion-server compile` then reads the staged tree, so it computes the
artifact's own content hash over exactly what will be applied -- which is why the settings go in
here rather than into the finished artifact, whose hash is a projection the CLI owns.

Why the settings cannot live in the definitions: `allow_private_urls` is a BOOLEAN and a connector
`url` is SCHEME-CHECKED, and both are validated by every offline gate (`lint`, `clippy`, `compile`,
`package lint`) BEFORE `var://` references are resolved. A definition using one is valid on a
running node and unlintable everywhere else, so the deployment applies them and the committed
package stays lintable and free of any deployment's addresses.

`--drop=<path>` leaves one staged file out, relative to the set: how a runner loads fewer match
lanes than the package ships. It is applied before the version is computed, so the version names
what is actually applied, and load-package.sh's retire sweep then removes a lane a previous load left.
"""
import hashlib
import json
import pathlib
import shutil
import sys


def main():
    src, stage, *args = sys.argv[1:]
    src, stage = pathlib.Path(src), pathlib.Path(stage)
    drops = [a.split("=", 1)[1] for a in args if a.startswith("--drop=")]
    settings = [a for a in args if not a.startswith("--drop=")]

    if stage.exists():
        shutil.rmtree(stage)
    # `plugins` IS copied: a package's wasm components and their plugin.toml manifests are part of
    # the set, and `compile` reads them into the artifact. Only what cannot be a definition is left
    # behind.
    shutil.copytree(src, stage, ignore=shutil.ignore_patterns(
        ".git", "scripts", "docs", "migrations", "*.md", "*.sh", "Dockerfile", "target",
        "*.rs", "Cargo.toml", "Cargo.lock", "rustfmt.toml"))

    for rel in drops:
        target = stage / rel
        if not target.is_file():
            sys.exit(f"stage_set: --drop names {rel}, which is not in {src}")
        target.unlink()
    for s in settings:
        cid, key, value = s.split("=", 2)
        if value == "":
            continue
        hits = [p for p in stage.rglob("*.json")
                if (d := json.loads(p.read_text())).get("id") == cid and "connector_type" in d]
        if not hits:
            sys.exit(f"stage_set: no connector '{cid}' under {src}")
        p = hits[0]
        d = json.loads(p.read_text())
        d["config"][key] = True if value == "true" else False if value == "false" else value
        p.write_text(json.dumps(d, indent=2) + "\n")

    body = b"".join(
        p.relative_to(stage).as_posix().encode() + b"\0" + p.read_bytes()
        for p in sorted(stage.rglob("*.json")))
    print("0.1.0-" + hashlib.sha256(body).hexdigest()[:12])


main()
