#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SOURCE_URL = "https://coeiroink.com/download"


def _read_text(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        request = urllib.request.Request(
            source,
            headers={"User-Agent": "voicevox-discord-coeiroink-installer/1.0"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8")
    return Path(source).read_text(encoding="utf-8")


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "voicevox-discord-coeiroink-installer/1.0"},
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                with dest.open("wb") as f:
                    shutil.copyfileobj(response, f)
            return
        except Exception as e:
            last_error = e
            if attempt < 3:
                time.sleep(3 * attempt)
    raise RuntimeError(f"download failed: {url}") from last_error


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep):
                raise RuntimeError(f"unsafe zip entry: {member.filename}")
        zf.extractall(dest)


def _copytree_contents(
    src: Path,
    dest: Path,
    *,
    skip_names: set[str] | None = None,
) -> None:
    skip_names = skip_names or set()
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in skip_names:
            continue
        target = dest / child.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _clean_directory_contents(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in dest.iterdir():
        if child.name == "lost+found":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _extract_next_data(html_text: str) -> dict:
    marker = '<script id="__NEXT_DATA__" type="application/json">'
    start = html_text.find(marker)
    if start == -1:
        raise RuntimeError("__NEXT_DATA__ script was not found")
    start += len(marker)
    end = html_text.find("</script>", start)
    if end == -1:
        raise RuntimeError("__NEXT_DATA__ script was not closed")
    return json.loads(html.unescape(html_text[start:end]))


def _load_speakers(source: str) -> list[dict]:
    text = _read_text(source)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict) and "downloadableSpeakers" in data:
            return data["downloadableSpeakers"]
        if isinstance(data, dict) and "props" in data:
            return data["props"]["pageProps"]["downloadableSpeakers"]
        raise RuntimeError("JSON source does not contain downloadableSpeakers")

    next_data = _extract_next_data(text)
    return next_data["props"]["pageProps"]["downloadableSpeakers"]


def _find_metas_dir(root: Path) -> Path:
    matches = sorted(root.rglob("metas.json"))
    if not matches:
        raise RuntimeError("metas.json was not found in meta zip")
    return matches[0].parent


def _find_style_dir(root: Path, style_id: str) -> Path:
    candidates = sorted(path.parent for path in root.rglob("config.yaml"))
    if not candidates:
        raise RuntimeError(f"config.yaml was not found for style {style_id}")
    for candidate in candidates:
        if candidate.name == style_id:
            return candidate
    return candidates[0]


def _style_installed(style_dir: Path) -> bool:
    return (style_dir / "config.yaml").exists()


def _filter_meta_styles(speaker_dir: Path) -> int:
    meta_path = speaker_dir / "metas.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    installed = {
        path.name for path in (speaker_dir / "model").iterdir() if path.is_dir()
    }
    styles = meta.get("styles", [])
    meta["styles"] = [
        style
        for style in styles
        if str(style.get("styleId", style.get("id"))) in installed
    ]
    if not meta["styles"]:
        raise RuntimeError(f"no installed styles matched metas.json: {speaker_dir}")
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    return len(meta["styles"])


def _split_prefixes(raw: str) -> set[str]:
    return {item for item in raw.replace(",", " ").split() if item}


def install_speakers(
    *,
    source: str,
    engine_root: Path,
    prefixes: set[str],
    clean: bool,
) -> None:
    speakers = _load_speakers(source)
    if prefixes:
        speakers = [
            speaker for speaker in speakers if speaker.get("prefix") in prefixes
        ]
    if not speakers:
        raise RuntimeError("no COEIROINK speakers selected")

    total_styles = 0
    speaker_info_dir = engine_root / "speaker_info"
    speaker_info_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        _clean_directory_contents(speaker_info_dir)

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for speaker in speakers:
            name = speaker["speakerName"]
            speaker_uuid = speaker["speakerUuid"]
            speaker_dir = speaker_info_dir / speaker_uuid
            model_dir = speaker_dir / "model"
            model_dir.mkdir(parents=True, exist_ok=True)

            print(f"Installing COEIROINK speaker: {name} ({speaker_uuid})")
            meta_zip = tmp / f"{speaker_uuid}-meta.zip"
            meta_extract = tmp / f"{speaker_uuid}-meta"
            _download(speaker["metaDownloadUrl"], meta_zip)
            meta_extract.mkdir()
            _safe_extract(meta_zip, meta_extract)
            # Refresh metadata every run so interrupted installs can resume with a
            # complete metas.json while preserving already downloaded model dirs.
            _copytree_contents(
                _find_metas_dir(meta_extract),
                speaker_dir,
                skip_names={"model"},
            )
            model_dir.mkdir(parents=True, exist_ok=True)

            for style in speaker["styles"]:
                style_id = str(style["styleId"])
                dest = model_dir / style_id
                if _style_installed(dest):
                    print(f"Skipping installed COEIROINK style: {name} ({style_id})")
                    continue

                style_zip = tmp / f"{speaker_uuid}-{style_id}.zip"
                style_extract = tmp / f"{speaker_uuid}-{style_id}"
                _download(style["downloadUrl"], style_zip)
                style_extract.mkdir()
                _safe_extract(style_zip, style_extract)
                style_dir = _find_style_dir(style_extract, style_id)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(style_dir, dest)
                shutil.rmtree(style_extract)
                style_zip.unlink()

            installed_count = _filter_meta_styles(speaker_dir)
            total_styles += installed_count
            shutil.rmtree(meta_extract)
            meta_zip.unlink()

    print(f"Installed COEIROINK speakers: {len(speakers)}")
    print(f"Installed COEIROINK styles: {total_styles}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--engine-root", default="/opt/coeiroink_engine")
    parser.add_argument("--prefixes", default="")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    install_speakers(
        source=args.source,
        engine_root=Path(args.engine_root),
        prefixes=_split_prefixes(args.prefixes),
        clean=args.clean,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
