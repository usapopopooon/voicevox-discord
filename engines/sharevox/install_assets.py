#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import pyopenjtalk

INSTALLER_VERSION = "1"
RESOURCE_REPO = "https://github.com/SHAREVOX/sharevox_resource.git"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _arch() -> tuple[str, str, str]:
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return machine, "arm64", "aarch64"
    return machine, "x64", "x64"


def _manifest_payload() -> dict[str, str]:
    machine, core_arch, ort_arch = _arch()
    return {
        "installer_version": INSTALLER_VERSION,
        "machine": machine,
        "core_arch": core_arch,
        "onnxruntime_arch": ort_arch,
        "engine_version": _env("SHAREVOX_ENGINE_VERSION", "0.2.1"),
        "resource_version": _env("SHAREVOX_RESOURCE_VERSION", "0.2.1"),
        "core_version": _env("SHAREVOX_CORE_VERSION", "0.2.1"),
        "model_version": _env("SHAREVOX_MODEL_VERSION", "0.2.1"),
        "onnxruntime_version": _env("ONNXRUNTIME_VERSION", "1.12.1"),
    }


def _required_assets_exist(data_dir: Path) -> bool:
    checks = [
        data_dir / "speaker_info",
        data_dir / "core",
        data_dir / "onnxruntime" / "lib",
        data_dir / "model" / "libraries.json",
        data_dir / "engine" / "engine_manifest.json",
        data_dir / "engine" / "engine_manifest_assets",
    ]
    return all(path.exists() for path in checks)


def _manifest_matches(manifest_path: Path, payload: dict[str, str]) -> bool:
    if not manifest_path.exists():
        return False
    try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return current == payload


def _clean_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.name == "lost+found":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copytree_contents(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "voicevox-discord-sharevox-installer/1.0"},
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                with dest.open("wb") as f:
                    shutil.copyfileobj(response, f)
            return
        except Exception as e:
            last_error = e
            if attempt < 3:
                time.sleep(5 * attempt)
    raise RuntimeError(f"download failed: {url}") from last_error


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep):
                raise RuntimeError(f"unsafe zip entry: {member.filename}")
        zf.extractall(dest)


def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with tarfile.open(tar_path) as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest_resolved) + os.sep):
                raise RuntimeError(f"unsafe tar entry: {member.name}")
        tf.extractall(dest)


def _write_engine_version(engine_root: Path, version: str) -> None:
    init_path = engine_root / "voicevox_engine" / "__init__.py"
    if init_path.exists():
        text = init_path.read_text(encoding="utf-8")
        lines = [
            f'__version__ = "{version}"' if line.startswith("__version__ = ") else line
            for line in text.splitlines()
        ]
        init_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ensure_openjtalk_dict() -> None:
    raw_dir = pyopenjtalk.OPEN_JTALK_DICT_DIR
    dict_dir = Path(raw_dir.decode("utf-8") if isinstance(raw_dir, bytes) else raw_dir)
    if (dict_dir / "sys.dic").exists():
        print(f"OpenJTalk dictionary already exists in {dict_dir}")
        return

    dict_dir.mkdir(parents=True, exist_ok=True)
    print(f"Installing OpenJTalk dictionary into {dict_dir}")
    pyopenjtalk._extract_dic()
    if not (dict_dir / "sys.dic").exists():
        raise RuntimeError(f"OpenJTalk dictionary install failed: {dict_dir}")


def _install_resource(tmp: Path, data_dir: Path, payload: dict[str, str]) -> None:
    if (
        (data_dir / "speaker_info").exists()
        and (data_dir / "engine" / "engine_manifest.json").exists()
        and (data_dir / "engine" / "engine_manifest_assets").exists()
    ):
        print("Skipping installed SHAREVOX resource")
        return

    for path in (
        data_dir / "speaker_info",
        data_dir / "engine",
    ):
        if path.exists():
            shutil.rmtree(path)

    resource_dir = tmp / "sharevox_resource"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            payload["resource_version"],
            RESOURCE_REPO,
            str(resource_dir),
        ],
        check=True,
    )
    shutil.copytree(resource_dir / "character_info", data_dir / "speaker_info")

    engine_data = data_dir / "engine"
    engine_data.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resource_dir / "engine" / "README.md", engine_data / "README.md")
    manifest = json.loads(
        (resource_dir / "engine" / "engine_manifest.json").read_text(
            encoding="utf-8",
        ),
    )
    manifest["version"] = payload["engine_version"]
    (engine_data / "engine_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copytree(
        resource_dir / "engine" / "engine_manifest_assets",
        engine_data / "engine_manifest_assets",
    )


def _install_core(tmp: Path, data_dir: Path, payload: dict[str, str]) -> None:
    core_dir = data_dir / "core"
    if core_dir.exists() and any(core_dir.iterdir()):
        print("Skipping installed SHAREVOX core")
        return
    if core_dir.exists():
        shutil.rmtree(core_dir)

    core_asset = (
        f"sharevox_core-linux-{payload['core_arch']}-cpu-{payload['core_version']}"
    )
    zip_path = tmp / "sharevox_core.zip"
    _download(
        "https://github.com/SHAREVOX/sharevox_core/releases/download/"
        f"{payload['core_version']}/{core_asset}.zip",
        zip_path,
    )
    extract_dir = tmp / "sharevox_core"
    extract_dir.mkdir()
    _safe_extract_zip(zip_path, extract_dir)
    shutil.copytree(extract_dir / core_asset, data_dir / "core")


def _install_onnxruntime(tmp: Path, data_dir: Path, payload: dict[str, str]) -> None:
    runtime_dir = data_dir / "onnxruntime"
    if (runtime_dir / "lib").exists():
        print("Skipping installed ONNX Runtime")
        return
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)

    archive = (
        "onnxruntime-linux-"
        f"{payload['onnxruntime_arch']}-{payload['onnxruntime_version']}"
    )
    tar_path = tmp / "onnxruntime.tgz"
    _download(
        "https://github.com/microsoft/onnxruntime/releases/download/"
        f"v{payload['onnxruntime_version']}/{archive}.tgz",
        tar_path,
    )
    extract_dir = tmp / "onnxruntime"
    extract_dir.mkdir()
    _safe_extract_tar(tar_path, extract_dir)
    shutil.copytree(extract_dir / archive, data_dir / "onnxruntime")


def _install_model(tmp: Path, data_dir: Path, payload: dict[str, str]) -> None:
    model_dir = data_dir / "model"
    if (model_dir / "libraries.json").exists():
        print("Skipping installed SHAREVOX model")
        return
    if model_dir.exists():
        shutil.rmtree(model_dir)

    model_asset = f"sharevox_model-{payload['model_version']}"
    zip_path = tmp / "sharevox_model.zip"
    _download(
        "https://github.com/SHAREVOX/sharevox_core/releases/download/"
        f"{payload['model_version']}/{model_asset}.zip",
        zip_path,
    )
    extract_dir = tmp / "sharevox_model"
    extract_dir.mkdir()
    _safe_extract_zip(zip_path, extract_dir)
    libraries = next(extract_dir.rglob("libraries.json"), None)
    if libraries is None:
        raise RuntimeError("libraries.json was not found in SHAREVOX model zip")
    shutil.copytree(libraries.parent, data_dir / "model")


def _replace_with_symlink(link_path: Path, target: Path) -> None:
    if link_path.is_symlink() and link_path.resolve() == target.resolve():
        return
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir() and not link_path.is_symlink():
            shutil.rmtree(link_path)
        else:
            link_path.unlink()
    link_path.symlink_to(target, target_is_directory=target.is_dir())


def _setup_runtime_paths(data_dir: Path, engine_root: Path) -> None:
    _replace_with_symlink(engine_root / "speaker_info", data_dir / "speaker_info")
    _replace_with_symlink(engine_root / "model", data_dir / "model")
    _replace_with_symlink(engine_root / "README.md", data_dir / "engine" / "README.md")
    _replace_with_symlink(
        engine_root / "engine_manifest.json",
        data_dir / "engine" / "engine_manifest.json",
    )
    _replace_with_symlink(
        engine_root / "engine_manifest_assets",
        data_dir / "engine" / "engine_manifest_assets",
    )
    _replace_with_symlink(Path("/opt/sharevox_core"), data_dir / "core")
    _replace_with_symlink(Path("/opt/onnxruntime"), data_dir / "onnxruntime")


def install_assets(data_dir: Path, engine_root: Path, force: bool) -> None:
    _ensure_openjtalk_dict()

    payload = _manifest_payload()
    manifest_path = data_dir / ".voicevox-discord-sharevox-manifest.json"
    needs_install = (
        force
        or not _required_assets_exist(data_dir)
        or not _manifest_matches(manifest_path, payload)
    )

    if needs_install:
        print(f"Installing SHAREVOX assets into {data_dir}", flush=True)
        if force or manifest_path.exists():
            _clean_directory(data_dir)
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            _install_resource(tmp, data_dir, payload)
            _install_core(tmp, data_dir, payload)
            _install_onnxruntime(tmp, data_dir, payload)
            _install_model(tmp, data_dir, payload)
        if not _required_assets_exist(data_dir):
            raise RuntimeError(f"SHAREVOX asset install is incomplete: {data_dir}")
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        print(f"SHAREVOX assets already exist in {data_dir}; skipping download")

    _write_engine_version(engine_root, payload["engine_version"])
    _setup_runtime_paths(data_dir, engine_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    install_assets(
        data_dir=Path(_env("SHAREVOX_DATA_DIR", "/opt/sharevox_data")),
        engine_root=Path(_env("SHAREVOX_ENGINE_ROOT", "/opt/sharevox_engine")),
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
