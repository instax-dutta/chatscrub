from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import PipelineConfig, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="chatscrub",
        description="Scrub chat exports into an anonymized, safety-filtered corpus.",
    )
    ap.add_argument("--config", "-c", required=True, help="TOML config file")
    ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config).expanduser().resolve()
    cfg = PipelineConfig.from_toml(str(cfg_path))

    # Resolve relative paths against the config file's directory.
    base = cfg_path.parent
    cfg.out = str((base / cfg.out).resolve())
    cfg.manifest = str((base / cfg.manifest).resolve())
    cfg.exclude = set(cfg.exclude)  # type: ignore[assignment]

    run(cfg, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
