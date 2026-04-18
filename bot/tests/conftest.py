import pytest


@pytest.fixture(autouse=True)
def env_setup(monkeypatch):
    """テスト用の環境変数をセット"""
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("VOICEVOX_URL", "http://test-voicevox:50021")
    monkeypatch.setenv("DEFAULT_SPEAKER_ID", "3")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


@pytest.fixture(autouse=True)
def default_speaker_registered(env_setup):
    """synthesize が動くよう DEFAULT_SPEAKER のマッピングを登録。
    speaker_engine 空の動作を検証するテストは明示的に clear すること。
    env_setup に依存して ENGINES 計算後に登録する。"""
    import bot

    bot.speaker_engine.setdefault(3, ("http://test-voicevox:50021", 3))
    yield
    # 後片付けはテスト側の try/finally 任せ（fetch_speakers 系テストが clear する）
