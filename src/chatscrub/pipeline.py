"""Config loading, room labelling, and idempotent manifest state."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .scrub import (CompiledScrub, ScrubConfig, default_topic_rules, process_file)


@dataclass
class PipelineConfig:
    input_globs: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    scrub: ScrubConfig = field(default_factory=ScrubConfig)
    label_overrides: dict[str, str] = field(default_factory=dict)
    out: str = "scrubbed.jsonl"
    manifest: str = ".processed_manifest"
    min_len: int = 60
    max_len: int = 350
    max_dupes: int = 3
    author_cap: int = 150
    small_circle_authors: int = 150
    real_text_floor: int = 25
    extra_blocklist: list[str] = field(default_factory=list)

    @classmethod
    def from_toml(cls, path: str) -> "PipelineConfig":
        with open(path, "rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
        paths = raw.get("paths", {})
        filts = raw.get("filters", {})
        caps = raw.get("caps", {})
        ident = raw.get("identity", {})
        scrub = ScrubConfig(
            server_names=ident.get("server_names", []),
            creator_names=ident.get("creator_names", []),
            emote_prefixes=ident.get("emote_prefixes", []),
            emote_interior=ident.get("emote_interior", []),
            extra_blocklist=filts.get("extra_blocklist", []),
        )
        return cls(
            input_globs=paths.get("input_globs", []),
            exclude=set(paths.get("exclude", [])),  # type: ignore[arg-type]
            scrub=scrub,
            label_overrides=raw.get("labels", {}).get("overrides", {}),
            out=paths.get("out", "scrubbed.jsonl"),
            manifest=paths.get("manifest", ".processed_manifest"),
            min_len=int(filts.get("min_len", 60)),
            max_len=int(filts.get("max_len", 350)),
            max_dupes=int(caps.get("max_dupes", 3)),
            author_cap=int(caps.get("author_cap", 150)),
            small_circle_authors=int(caps.get("small_circle_authors", 150)),
            real_text_floor=int(caps.get("real_text_floor", 25)),
        )


def room_label(path: str, overrides: dict[str, str] | None = None) -> str:
    """Neutral room labels. Unknown files get room-<hash>, never a server name."""
    stem = os.path.basename(path)
    core = stem
    for junk in ("〢", "┃"):
        core = core.replace(junk, "")
    core = re.sub(r"-part\d+", "", core)          # multi-part exports = one room
    core = re.sub(r"-\d{10,}(\.json)?$", "", core)  # timestamp suffix
    for needle, label in (overrides or {}).items():
        if needle in core:
            return label
    return "room-" + hashlib.sha256(core.encode()).hexdigest()[:6]


def load_manifest(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return set(fh.read().split())


def append_manifest(path: str, name: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(name + "\n")


def discover(cfg: PipelineConfig) -> list[str]:
    found: list[str] = []
    for pattern in cfg.input_globs:
        found.extend(glob.glob(os.path.expanduser(pattern)))
    return sorted(
        (p for p in found if os.path.basename(p) not in cfg.exclude),
        key=os.path.getmtime,
    )


def run(cfg: PipelineConfig, verbose: bool = True) -> dict[str, Any]:
    """Idempotent incremental run. Only unseen input files are processed."""
    scrub = cfg.scrub.compile()
    rules = default_topic_rules()

    rows: list[dict] = []
    manifest_path = cfg.manifest
    done = load_manifest(manifest_path)
    if os.path.exists(cfg.out):
        with open(cfg.out, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    seen = {r["id"] for r in rows}
    state = {"seen_text": Counter(r["text"] for r in rows)}
    if verbose:
        print(f"resuming with {len(rows)} existing rows", file=sys.stderr)

    fresh = [p for p in discover(cfg) if os.path.basename(p) not in done]
    if verbose:
        print(f"{len(fresh)} new file(s) to process", file=sys.stderr)

    added: Counter = Counter()
    for path in fresh:
        try:
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"SKIP {os.path.basename(path)}: {exc}", file=sys.stderr)
            continue
        label = room_label(path, cfg.label_overrides)
        kept, drop = process_file(
            path, label, scrub, set(), state,
            min_len=cfg.min_len, max_len=cfg.max_len,
            max_dupes=cfg.max_dupes, author_cap=cfg.author_cap,
            small_circle_authors=cfg.small_circle_authors,
            real_text_floor=cfg.real_text_floor, topic_rules=rules,
        )
        n_new = 0
        for r in kept:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            rows.append(r)
            n_new += 1
        added[os.path.basename(path)] = n_new
        if verbose:
            print(f"{label}: +{n_new} rows | dropped={dict(drop)}", file=sys.stderr)
        append_manifest(manifest_path, os.path.basename(path))

    with open(cfg.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    report = {
        "total_rows": len(rows),
        "by_source": dict(Counter(r["source"] for r in rows)),
        "by_file": dict(added),
        "unique_ids": len(seen),
    }
    if verbose:
        print(json.dumps(report, indent=2), file=sys.stderr)
    return report
