#!/usr/bin/env python3
"""Make goboscript's standard-library update non-fatal.

goboscript resolves the ``goboscript/std`` version at every build with an
unauthenticated GitHub API call when its cache is stale/absent
(``src/standard_library.rs``). Offline machines, firewalls, and rate-limited CI
IPs get an error object instead of the expected tag array, which aborts the
build ("invalid type: map, expected a sequence"). This patch falls back to
version 0.0.0 (which ``build.rs`` treats as "no standard library") on any
failure, so builds work without that network dependency.

Applied to the pinned commit before ``cargo build`` in
.github/workflows/goboscript-prebuilt.yml. Idempotent; asserts the anchor.
"""

from __future__ import annotations

import pathlib
import sys

PATH = pathlib.Path("goboscript/src/standard_library.rs")

OLD = """    let response = client
        .get("https://api.github.com/repos/goboscript/std/tags")
        .send()
        .context("Failed to fetch tags from GitHub")?;
    let tags = response.json::<Vec<Tag>>()?;
    let mut tags: Vec<_> = tags
        .into_iter()
        .map(|tag| Version::parse(tag.name.strip_prefix("v").unwrap()).unwrap())
        .collect();
    tags.sort();
    Ok(tags.last().unwrap().clone())"""

NEW = """    let response = match client
        .get("https://api.github.com/repos/goboscript/std/tags")
        .send()
    {
        Ok(response) => response,
        Err(_) => return Ok(Version::new(0, 0, 0)),
    };
    let tags = match response.json::<Vec<Tag>>() {
        Ok(tags) => tags,
        Err(_) => return Ok(Version::new(0, 0, 0)),
    };
    let mut tags: Vec<_> = tags
        .into_iter()
        .filter_map(|tag| Version::parse(tag.name.strip_prefix("v")?).ok())
        .collect();
    tags.sort();
    Ok(tags.last().cloned().unwrap_or_else(|| Version::new(0, 0, 0)))"""


def main() -> int:
    source = PATH.read_text(encoding="utf-8")
    if "Err(_) => return Ok(Version::new(0, 0, 0))" in source:
        print("standard_library.rs already patched")
        return 0
    if OLD not in source:
        raise SystemExit("anchor not found; upstream standard_library.rs changed")
    PATH.write_text(source.replace(OLD, NEW, 1), encoding="utf-8")
    print("patched standard_library.rs (offline-safe std update)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
