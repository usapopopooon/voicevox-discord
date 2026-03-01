import pytest


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """テスト用の環境変数をセット"""
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("VOICEVOX_URL", "http://test-voicevox:50021")
    monkeypatch.setenv("VOICEVOX_SPEAKER_ID", "3")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
