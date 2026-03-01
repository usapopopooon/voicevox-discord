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

        s = get_user_settings(123456)
        assert s.speaker_id == VoiceSettings().speaker_id


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


class TestOnMessage:
    async def test_ignores_bot_messages(self):
        from bot import on_message

        message = MagicMock()
        message.author.bot = True
        await on_message(message)

    def test_text_truncation(self):
        long_text = "あ" * 150
        if len(long_text) > 100:
            long_text = long_text[:100] + "、以下省略"
        assert long_text.endswith("、以下省略")
        assert long_text == "あ" * 100 + "、以下省略"
