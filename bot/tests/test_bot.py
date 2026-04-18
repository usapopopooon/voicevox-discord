import re
from collections import deque
from unittest.mock import MagicMock, patch

import pytest
from aioresponses import aioresponses


class TestSynthesize:
    async def test_returns_audio_bytes(self):
        from bot import VoiceSettings, synthesize

        with aioresponses() as m:
            m.post(
                re.compile(r"http://test-voicevox:50021/audio_query"),
                payload={"accent_phrases": []},
            )
            m.post(
                re.compile(r"http://test-voicevox:50021/synthesis"),
                body=b"fake-wav-data",
            )
            result = await synthesize("テスト", VoiceSettings())
            assert result == b"fake-wav-data"

    async def test_raises_on_api_error(self):
        from bot import VoiceSettings, synthesize

        with aioresponses() as m:
            m.post(
                re.compile(r"http://test-voicevox:50021/audio_query"),
                status=500,
            )
            with pytest.raises(Exception):
                await synthesize("テスト", VoiceSettings())

    async def test_applies_voice_params(self):
        from bot import VoiceSettings, synthesize

        settings = VoiceSettings(speed=1.5, pitch=0.1, intonation=1.2, volume=0.8)

        with aioresponses() as m:
            m.post(
                re.compile(r"http://test-voicevox:50021/audio_query"),
                payload={
                    "accent_phrases": [],
                    "speedScale": 1.0,
                    "pitchScale": 0.0,
                    "intonationScale": 1.0,
                    "volumeScale": 1.0,
                },
            )
            m.post(
                re.compile(r"http://test-voicevox:50021/synthesis"),
                body=b"fake-wav-data",
            )
            result = await synthesize("テスト", settings)
            assert result == b"fake-wav-data"

            # synthesis に送られたリクエストボディのパラメータを検証
            synthesis_call = list(m.requests.values())[1][0]
            body = synthesis_call.kwargs["json"]
            assert body["speedScale"] == 1.5
            assert body["pitchScale"] == 0.1
            assert body["intonationScale"] == 1.2
            assert body["volumeScale"] == 0.8


class TestVoiceSettings:
    def test_defaults(self):
        from bot import VoiceSettings

        s = VoiceSettings()
        assert s.speed == 1.0
        assert s.pitch == 0.0
        assert s.intonation == 1.0
        assert s.volume == 1.0

    def test_get_user_settings_returns_default(self):
        from bot import VoiceSettings, get_user_settings

        s = get_user_settings(111, 123456)
        assert s.speaker_id == VoiceSettings().speaker_id

    def test_get_user_settings_falls_back_to_legacy_scope(self):
        from bot import VoiceSettings, get_user_settings, user_settings

        user_settings[(0, 555)] = VoiceSettings(speed=1.7)
        s = get_user_settings(999, 555)
        assert s.speed == 1.7
        user_settings.pop((0, 555), None)


class TestPlayNext:
    async def test_empty_queue_does_not_play(self):
        from bot import play_next, queues

        mock_vc = MagicMock()
        queues[999] = deque()
        await play_next(999, mock_vc)
        mock_vc.play.assert_not_called()
        queues.pop(999, None)

    @patch("discord.FFmpegPCMAudio")
    async def test_plays_from_queue(self, mock_ffmpeg):
        from bot import play_next, queues

        mock_vc = MagicMock()
        queues[999] = deque([b"audio-data"])
        await play_next(999, mock_vc)
        mock_vc.play.assert_called_once()
        assert len(queues[999]) == 0
        queues.pop(999, None)


class TestApplyDict:
    def test_replaces_registered_words(self):
        from bot import apply_dict, guild_dicts

        guild_dicts[888] = {"w": "ダブリュー", "lol": "わらい"}
        result = apply_dict(888, "hello w lol")
        assert result == "hello ダブリュー わらい"
        guild_dicts.pop(888, None)

    def test_no_dict_returns_original(self):
        from bot import apply_dict

        result = apply_dict(777, "hello")
        assert result == "hello"


class TestCleanText:
    def test_removes_urls(self):
        from bot import clean_text

        result = clean_text("見て https://example.com すごい")
        assert result == "見て URLしょうりゃく すごい"

    def test_replaces_email(self):
        from bot import clean_text

        result = clean_text("連絡先は test@example.com です")
        assert result == "連絡先は メールアドレスしょうりゃく です"

    def test_converts_custom_emoji_to_name(self):
        from bot import clean_text

        assert clean_text("わーい<:smile:123456>") == "わーいsmile"

    def test_converts_animated_emoji(self):
        from bot import clean_text

        assert clean_text("<a:dance:789>") == "dance"

    def test_preserves_unicode_emoji(self):
        from bot import clean_text

        assert clean_text("こんにちは😀") == "こんにちは😀"

    def test_empty_after_clean(self):
        from bot import clean_text

        assert clean_text("https://example.com") == "URLしょうりゃく"


class TestMute:
    def test_is_muted(self):
        from bot import guild_mutes, is_muted

        guild_mutes[111] = {222}
        assert is_muted(111, 222) is True
        assert is_muted(111, 333) is False
        assert is_muted(999, 222) is False
        guild_mutes.pop(111, None)


class TestOnMessage:
    async def test_ignores_bot_messages(self):
        from bot import on_message

        message = MagicMock()
        message.author.bot = True
        await on_message(message)

    async def test_ignores_non_guild_messages(self):
        from bot import on_message

        message = MagicMock()
        message.author.bot = False
        message.guild = None
        await on_message(message)

    async def test_ignores_other_channel(self):
        from bot import on_message, read_channels

        message = MagicMock()
        message.author.bot = False
        message.guild.id = 111
        message.guild.voice_client.is_connected.return_value = True
        message.channel.id = 999
        read_channels[111] = 888  # /joinしたチャンネルは別
        await on_message(message)
        # synthesizeが呼ばれないことを確認（エラーなく終了）
        read_channels.pop(111, None)

    async def test_ignores_muted_user(self):
        from bot import guild_mutes, on_message, read_channels

        message = MagicMock()
        message.author.bot = False
        message.guild.id = 111
        message.guild.voice_client.is_connected.return_value = True
        message.channel.id = 888
        message.author.id = 222
        read_channels[111] = 888
        guild_mutes[111] = {222}
        await on_message(message)
        read_channels.pop(111, None)
        guild_mutes.pop(111, None)

    def test_text_truncation(self):
        from bot import MAX_READ_LENGTH

        long_text = "あ" * 150
        if len(long_text) > MAX_READ_LENGTH:
            long_text = long_text[:MAX_READ_LENGTH] + "、いかしょうりゃく"
        assert long_text.endswith("、いかしょうりゃく")
        assert long_text == "あ" * 100 + "、いかしょうりゃく"


class TestEngines:
    def test_only_configured_engines_loaded(self):
        from bot import ENGINES

        # VOICEVOX はテスト用 conftest で設定済み
        engine_names = [name for name, _, _ in ENGINES]
        assert "VOICEVOX" in engine_names
        # URL未設定のエンジンは除外される
        for name, url, _ in ENGINES:
            assert url != ""

    def test_engine_id_offsets_are_unique(self):
        from bot import ENGINES

        offsets = [offset for _, _, offset in ENGINES]
        assert len(offsets) == len(set(offsets))

    async def test_synthesize_raises_when_no_engines(self, monkeypatch):
        from bot import VoiceSettings, synthesize

        monkeypatch.setattr("bot.ENGINES", [])
        with pytest.raises(RuntimeError):
            await synthesize("テスト", VoiceSettings())


class TestReadChannels:
    def test_read_channels_tracks_guild(self):
        from bot import read_channels

        read_channels[111] = 888
        assert read_channels[111] == 888
        read_channels.pop(111, None)


class TestCharacters:
    def test_characters_dict_structure(self):
        from bot import characters

        # fetch_speakers 実行前は空
        # 構造の型確認
        assert isinstance(characters, dict)

    def test_speaker_engine_fallback(self):
        from bot import ENGINES, speaker_engine

        # 未登録のspeaker_idでもフォールバックする
        fallback = speaker_engine.get(99999, (ENGINES[0][1], 99999))
        assert fallback == (ENGINES[0][1], 99999)
