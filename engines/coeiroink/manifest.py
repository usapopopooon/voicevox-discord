#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _payload(args: argparse.Namespace) -> dict[str, str]:
    return {
        "source": args.source,
        "prefixes": args.prefixes,
        "installer_version": args.installer_version,
    }


def check(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    if not manifest.exists():
        return 1
    try:
        current = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return 1
    return 0 if current == _payload(args) else 1


def write(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(_payload(args), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "write"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--manifest", required=True)
        subparser.add_argument("--source", required=True)
        subparser.add_argument("--prefixes", required=True)
        subparser.add_argument("--installer-version", required=True)
    args = parser.parse_args()
    if args.command == "check":
        return check(args)
    if args.command == "write":
        return write(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
