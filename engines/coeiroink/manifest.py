#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path


def _config(args: argparse.Namespace) -> dict[str, str]:
    return {
        "engine_ref": args.engine_ref,
        "source": args.source,
        "prefixes": args.prefixes,
        "installer_version": args.installer_version,
    }


def _style_complete(style_dir: Path) -> bool:
    return (style_dir / "config.yaml").is_file() and any(style_dir.rglob("*.pth"))


def _inventory(speaker_info_dir: Path) -> dict[str, list[str]]:
    inventory: dict[str, list[str]] = {}
    for meta_path in sorted(speaker_info_dir.glob("*/metas.json")):
        speaker_dir = meta_path.parent
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            styles = meta["styles"]
            style_ids = sorted(
                str(style.get("styleId", style.get("id"))) for style in styles
            )
        except Exception as error:
            raise RuntimeError(f"invalid COEIROINK metadata: {meta_path}") from error
        if not style_ids or "None" in style_ids:
            raise RuntimeError(f"no valid COEIROINK styles: {meta_path}")
        for style_id in style_ids:
            style_dir = speaker_dir / "model" / style_id
            if not _style_complete(style_dir):
                raise RuntimeError(f"incomplete COEIROINK style: {style_dir}")
        inventory[speaker_dir.name] = style_ids
    if not inventory:
        raise RuntimeError(f"no COEIROINK speakers: {speaker_info_dir}")
    return inventory


def _manifest_payload(args: argparse.Namespace) -> dict[str, object]:
    return {
        "config": _config(args),
        "inventory": _inventory(Path(args.speaker_info_dir)),
    }


def cache_key(args: argparse.Namespace) -> int:
    encoded = json.dumps(
        _config(args),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    print(hashlib.sha256(encoded).hexdigest()[:20])
    return 0


def check(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    if not manifest.exists():
        return 1
    try:
        current = json.loads(manifest.read_text(encoding="utf-8"))
        expected = _manifest_payload(args)
    except Exception:
        return 1
    return 0 if current == expected else 1


def write(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(_manifest_payload(args), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest)
    return 0


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def activate(args: argparse.Namespace) -> int:
    cache_root = Path(args.cache_root).resolve()
    release_dir = Path(args.release_dir).resolve()
    releases_root = cache_root / "releases"
    if release_dir.parent != releases_root or not release_dir.is_dir():
        raise RuntimeError(f"invalid COEIROINK release directory: {release_dir}")

    current = cache_root / "current"
    temporary = cache_root / f".current-{os.getpid()}"
    _remove_path(temporary)
    temporary.symlink_to(release_dir, target_is_directory=True)
    os.replace(temporary, current)

    engine_speaker_info = Path(args.engine_root) / "speaker_info"
    _remove_path(engine_speaker_info)
    engine_speaker_info.symlink_to(current, target_is_directory=True)
    return 0


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine-ref", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--prefixes", required=True)
    parser.add_argument("--installer-version", required=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    key_parser = subparsers.add_parser("cache-key")
    _add_config_arguments(key_parser)

    for command in ("check", "write"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--manifest", required=True)
        subparser.add_argument("--speaker-info-dir", required=True)
        _add_config_arguments(subparser)

    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--cache-root", required=True)
    activate_parser.add_argument("--release-dir", required=True)
    activate_parser.add_argument("--engine-root", required=True)

    args = parser.parse_args()
    if args.command == "cache-key":
        return cache_key(args)
    if args.command == "check":
        return check(args)
    if args.command == "write":
        return write(args)
    if args.command == "activate":
        return activate(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
