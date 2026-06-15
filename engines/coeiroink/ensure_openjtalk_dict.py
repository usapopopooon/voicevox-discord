#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import pyopenjtalk


def main() -> int:
    raw_dir = pyopenjtalk.OPEN_JTALK_DICT_DIR
    dict_dir = Path(raw_dir.decode("utf-8") if isinstance(raw_dir, bytes) else raw_dir)
    if (dict_dir / "sys.dic").exists():
        print(f"OpenJTalk dictionary already exists in {dict_dir}")
        return 0

    dict_dir.mkdir(parents=True, exist_ok=True)
    print(f"Installing OpenJTalk dictionary into {dict_dir}")
    pyopenjtalk._extract_dic()
    if not (dict_dir / "sys.dic").exists():
        raise RuntimeError(f"OpenJTalk dictionary install failed: {dict_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
