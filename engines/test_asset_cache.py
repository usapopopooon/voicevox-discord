from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coeiroink_manifest = _load_module(
    "coeiroink_manifest",
    ROOT / "coeiroink" / "manifest.py",
)
sharevox_assets = _load_module(
    "sharevox_assets",
    ROOT / "sharevox" / "install_assets.py",
)


@pytest.mark.parametrize(
    "dockerfile",
    [ROOT / "coeiroink" / "Dockerfile", ROOT / "sharevox" / "Dockerfile"],
)
def test_engine_builds_retry_and_cache_pip_downloads(dockerfile: Path) -> None:
    contents = dockerfile.read_text(encoding="utf-8")

    assert "PIP_DEFAULT_TIMEOUT=300" in contents
    assert "PIP_RETRIES=10" in contents
    assert "--mount=type=cache,target=/root/.cache/pip" in contents
    assert "pip install --no-cache-dir" not in contents


def _coeiroink_args(release: Path) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=str(release / ".voicevox-discord-coeiroink-manifest.json"),
        speaker_info_dir=str(release),
        engine_ref="test-engine-ref",
        source="https://example.invalid/speakers",
        prefixes="all",
        installer_version="3",
    )


def _complete_coeiroink_release(release: Path) -> Path:
    style_dir = release / "speaker-uuid" / "model" / "1"
    style_dir.mkdir(parents=True)
    (release / "speaker-uuid" / "metas.json").write_text(
        json.dumps({"styles": [{"styleId": 1}]}),
        encoding="utf-8",
    )
    (style_dir / "config.yaml").write_text("name: test\n", encoding="utf-8")
    model = style_dir / "model.pth"
    model.write_bytes(b"model")
    return model


def test_coeiroink_manifest_detects_missing_style_asset(tmp_path: Path) -> None:
    release = tmp_path / "cache" / "releases" / "release-a"
    model = _complete_coeiroink_release(release)
    args = _coeiroink_args(release)

    assert coeiroink_manifest.write(args) == 0
    assert coeiroink_manifest.check(args) == 0

    model.unlink()
    assert coeiroink_manifest.check(args) == 1


def test_coeiroink_activation_switches_only_complete_release(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    release = cache_root / "releases" / "release-a"
    _complete_coeiroink_release(release)
    engine_root = tmp_path / "engine"
    (engine_root / "speaker_info").mkdir(parents=True)
    args = argparse.Namespace(
        cache_root=str(cache_root),
        release_dir=str(release),
        engine_root=str(engine_root),
    )

    assert coeiroink_manifest.activate(args) == 0
    assert (cache_root / "current").resolve() == release.resolve()
    assert (engine_root / "speaker_info").resolve() == release.resolve()


def test_sharevox_failed_install_preserves_active_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_root = tmp_path / "cache"
    old_release = cache_root / "releases" / "old"
    old_release.mkdir(parents=True)
    current = cache_root / "current"
    current.symlink_to(old_release, target_is_directory=True)

    monkeypatch.setattr(sharevox_assets, "_ensure_openjtalk_dict", lambda: None)
    monkeypatch.setattr(
        sharevox_assets,
        "_install_resource",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("download failed")),
    )

    with pytest.raises(RuntimeError, match="download failed"):
        sharevox_assets.install_assets(
            cache_root=cache_root,
            engine_root=tmp_path / "engine",
            force=False,
        )

    assert current.resolve() == old_release.resolve()


def test_sharevox_activation_replaces_symlink_atomically(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    old_release = cache_root / "releases" / "old"
    new_release = cache_root / "releases" / "new"
    old_release.mkdir(parents=True)
    new_release.mkdir(parents=True)
    current = cache_root / "current"
    current.symlink_to(old_release, target_is_directory=True)

    activated = sharevox_assets._activate_release(cache_root, new_release)

    assert activated == current
    assert current.resolve() == new_release.resolve()
    assert old_release.is_dir()
