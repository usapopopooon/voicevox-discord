#!/usr/bin/env python3
from __future__ import annotations

import shutil
from pathlib import Path

import pyopenjtalk


def _dict_complete(dict_dir: Path) -> bool:
    required = ("char.bin", "matrix.bin", "sys.dic", "unk.dic")
    return all((dict_dir / name).exists() for name in required)


def main() -> int:
    raw_dir = pyopenjtalk.OPEN_JTALK_DICT_DIR
    dict_dir = Path(raw_dir.decode("utf-8") if isinstance(raw_dir, bytes) else raw_dir)
    if _dict_complete(dict_dir):
        print(f"OpenJTalk dictionary already exists in {dict_dir}")
        return 0

    if dict_dir.exists():
        for child in dict_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    dict_dir.mkdir(parents=True, exist_ok=True)
    print(f"Installing OpenJTalk dictionary into {dict_dir}")
    pyopenjtalk._extract_dic()
    if not _dict_complete(dict_dir):
        raise RuntimeError(f"OpenJTalk dictionary install failed: {dict_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
