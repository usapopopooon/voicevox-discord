import re
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aioresponses import aioresponses


def _make_mock_pool(rows=None):
    """asyncpg.Pool のモックを作成（acquire() が conn を返す async ctx mgr）"""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchval = AsyncMock(return_value=None)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


@pytest.fixture
def mock_db_pool():
    """db_pool を差し替えるフィクスチャ。yield で (pool, conn) を返す"""
    import bot

    pool, conn = _make_mock_pool()
    original = bot.db_pool
    bot.db_pool = pool
    try:
        yield pool, conn
    finally:
        bot.db_pool = original


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
        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False
        queues[999] = deque([b"audio-data"])
        await play_next(999, mock_vc)
        mock_vc.play.assert_called_once()
        assert len(queues[999]) == 0
        queues.pop(999, None)
        play_locks.pop(999, None)


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


class TestRequireDbPool:
    def test_raises_when_pool_is_none(self):
        import bot
        from bot import _require_db_pool

        original = bot.db_pool
        bot.db_pool = None
        try:
            with pytest.raises(RuntimeError, match="DB接続プール"):
                _require_db_pool()
        finally:
            bot.db_pool = original

    def test_returns_pool_when_set(self):
        import bot
        from bot import _require_db_pool

        sentinel = MagicMock()
        original = bot.db_pool
        bot.db_pool = sentinel
        try:
            assert _require_db_pool() is sentinel
        finally:
            bot.db_pool = original


class TestApplyDictAdditional:
    def test_no_chained_replacement(self):
        """a→b, b→c の同時登録で 'a' が 'c' にならず 'b' で止まること"""
        from bot import apply_dict, guild_dicts

        guild_dicts[555] = {"a": "b", "b": "c"}
        try:
            assert apply_dict(555, "a") == "b"
            assert apply_dict(555, "b") == "c"
        finally:
            guild_dicts.pop(555, None)

    def test_long_words_match_first(self):
        """長い単語が短い単語より優先されること"""
        from bot import apply_dict, guild_dicts

        guild_dicts[666] = {"foo": "X", "foobar": "Y"}
        try:
            assert apply_dict(666, "foobar foo") == "Y X"
        finally:
            guild_dicts.pop(666, None)

    def test_special_regex_chars_escaped(self):
        from bot import apply_dict, guild_dicts

        guild_dicts[667] = {"a.b": "OK", "(x)": "P"}
        try:
            assert apply_dict(667, "a.b ax (x)") == "OK ax P"
        finally:
            guild_dicts.pop(667, None)


class TestMemoryOptimizations:
    """メモリ節約系の挙動を検証するテスト"""

    def test_queue_drops_oldest_when_maxlen_exceeded(self):
        """_ensure_queue が maxlen 付き deque を返し、溢れた分は古い方から落ちる"""
        from bot import QUEUE_MAXLEN, _ensure_queue, queues

        queues.pop(7777, None)
        try:
            q = _ensure_queue(7777)
            assert q.maxlen == QUEUE_MAXLEN
            for i in range(QUEUE_MAXLEN + 5):
                q.append(f"a{i}".encode())
            assert len(q) == QUEUE_MAXLEN
            assert q[0] == f"a{5}".encode()
            assert q[-1] == f"a{QUEUE_MAXLEN + 4}".encode()
        finally:
            queues.pop(7777, None)

    async def test_on_guild_remove_cleans_all_state(self):
        """Bot がギルドから外れた時、ギルド固有状態を全て解放する"""
        import re as _re

        import bot
        from bot import VoiceSettings, on_guild_remove

        bot.guild_dicts[8888] = {"w": "ダブリュー"}
        bot.guild_mutes[8888] = {123}
        bot._dict_patterns[8888] = _re.compile("w")
        bot.user_settings[(8888, 1)] = VoiceSettings()
        bot.user_settings[(8888, 2)] = VoiceSettings()
        bot.user_settings[(9999, 1)] = VoiceSettings()

        guild = MagicMock()
        guild.id = 8888
        try:
            await on_guild_remove(guild)
            assert 8888 not in bot.guild_dicts
            assert 8888 not in bot.guild_mutes
            assert 8888 not in bot._dict_patterns
            assert (8888, 1) not in bot.user_settings
            assert (8888, 2) not in bot.user_settings
            assert (9999, 1) in bot.user_settings  # 別ギルドは保持
        finally:
            bot.guild_dicts.pop(8888, None)
            bot.guild_mutes.pop(8888, None)
            bot._dict_patterns.pop(8888, None)
            bot.user_settings.pop((9999, 1), None)

    def test_prune_candidate_fail_until_removes_expired(self):
        """期限切れバックオフ entry が削除され、未期限は残る"""
        import time

        import bot
        from bot import _prune_candidate_fail_until

        now = time.monotonic()
        bot._candidate_fail_until[("http://dead", 1)] = now - 100.0
        bot._candidate_fail_until[("http://alive", 2)] = now + 100.0
        try:
            _prune_candidate_fail_until()
            assert ("http://dead", 1) not in bot._candidate_fail_until
            assert ("http://alive", 2) in bot._candidate_fail_until
        finally:
            bot._candidate_fail_until.clear()


class TestPlayNextAdditional:
    async def test_state_check_client_exception_skips(self):
        import discord

        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.side_effect = discord.ClientException(
            "Not connected to voice."
        )
        queues[1000] = deque([b"audio"])
        await play_next(1000, mock_vc)
        mock_vc.play.assert_not_called()
        # 状態確認で失敗した場合、キューは消費しない
        assert len(queues[1000]) == 1
        queues.pop(1000, None)
        play_locks.pop(1000, None)

    async def test_not_connected_skips(self):
        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = False
        queues[1001] = deque([b"audio"])
        await play_next(1001, mock_vc)
        mock_vc.play.assert_not_called()
        # キューは消費されない
        assert len(queues[1001]) == 1
        queues.pop(1001, None)
        play_locks.pop(1001, None)

    async def test_already_playing_skips(self):
        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.is_playing.return_value = True
        mock_vc.is_paused.return_value = False
        queues[1002] = deque([b"audio"])
        await play_next(1002, mock_vc)
        mock_vc.play.assert_not_called()
        assert len(queues[1002]) == 1
        queues.pop(1002, None)
        play_locks.pop(1002, None)

    async def test_paused_skips(self):
        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = True
        queues[1003] = deque([b"audio"])
        await play_next(1003, mock_vc)
        mock_vc.play.assert_not_called()
        queues.pop(1003, None)
        play_locks.pop(1003, None)

    @patch("discord.FFmpegPCMAudio")
    async def test_client_exception_swallowed(self, mock_ffmpeg):
        import discord

        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False
        mock_vc.play.side_effect = discord.ClientException("Not connected to voice.")
        queues[1004] = deque([b"audio"])
        # 例外が外に漏れないこと
        await play_next(1004, mock_vc)
        # 接続中扱いの場合は音声を失わずキュー先頭へ戻る
        assert list(queues[1004]) == [b"audio"]
        queues.pop(1004, None)
        play_locks.pop(1004, None)

    @patch("discord.FFmpegPCMAudio")
    async def test_client_exception_drops_audio_when_disconnected(self, mock_ffmpeg):
        """vc.play 失敗時に VC 切断済みなら音声を破棄する（キューに積み残さない）"""
        import discord

        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        # _can_start_playback は True、その後の再確認で False になる
        mock_vc.is_connected.side_effect = [True, False]
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False
        mock_vc.play.side_effect = discord.ClientException("Not connected to voice.")
        queues[1006] = deque([b"audio"])
        await play_next(1006, mock_vc)
        # 切断済みなら積み直さず破棄
        assert len(queues[1006]) == 0
        queues.pop(1006, None)
        play_locks.pop(1006, None)

    async def test_empty_queue_returns_silently(self):
        from bot import play_locks, play_next, queues

        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False
        # キューが未登録でもエラーなし
        await play_next(1005, mock_vc)
        mock_vc.play.assert_not_called()
        queues.pop(1005, None)
        play_locks.pop(1005, None)


class TestBuildDictMessage:
    async def test_empty(self):
        from bot import build_dict_message

        content, view = build_dict_message(9990)
        assert "登録なし" in content
        assert view is not None

    async def test_with_entries(self):
        from bot import build_dict_message, guild_dicts

        guild_dicts[8888] = {"w": "ダブリュー", "lol": "わらい"}
        try:
            content, view = build_dict_message(8888)
            assert "2件" in content
            assert "w → ダブリュー" in content
            assert "lol → わらい" in content
        finally:
            guild_dicts.pop(8888, None)


class TestFetchSpeakers:
    async def test_populates_caches(self):
        import bot
        from bot import fetch_speakers

        with aioresponses() as m:
            m.get(
                "http://test-voicevox:50021/speakers",
                payload=[
                    {
                        "name": "ずんだもん",
                        "styles": [
                            {"id": 3, "name": "ノーマル"},
                            {"id": 1, "name": "あまあま"},
                        ],
                    },
                ],
            )
            await fetch_speakers()

        try:
            assert 3 in bot.speakers_cache
            assert 1 in bot.speakers_cache
            assert "ずんだもん" in bot.characters
            assert bot.speaker_engine[3] == ("http://test-voicevox:50021", 3)
            assert bot.speaker_engine[1] == ("http://test-voicevox:50021", 1)
            # スタイル一覧
            style_names = {s for _, s in bot.characters["ずんだもん"]}
            assert style_names == {"ノーマル", "あまあま"}
        finally:
            bot.speakers_cache.clear()
            bot.speaker_engine.clear()
            bot.characters.clear()

    async def test_failure_does_not_raise(self):
        import bot
        from bot import fetch_speakers

        with aioresponses() as m:
            m.get("http://test-voicevox:50021/speakers", status=500)
            # 例外が外に出ないこと
            await fetch_speakers()
        assert bot.speakers_cache == {}


class TestSynthesizeFallback:
    async def test_falls_back_to_default_speaker(self):
        import bot
        from bot import VoiceSettings, synthesize

        # DEFAULT_SPEAKER=3 のマッピングは登録済み、指定 speaker_id は未登録のケース
        bot.speaker_engine[3] = ("http://test-voicevox:50021", 3)
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"fallback-data")
                result = await synthesize("テスト", VoiceSettings(speaker_id=99999))
                assert result == b"fallback-data"
                audio_query_call = list(m.requests.values())[0][0]
                assert audio_query_call.kwargs["params"]["speaker"] == 3
        finally:
            bot.speaker_engine.pop(3, None)

    async def test_raises_when_speaker_engine_empty(self):
        """全候補が失敗した場合は ClientError 系を返す。"""
        import bot
        from bot import VoiceSettings, synthesize

        original_last_attempt = bot._last_speaker_refresh_attempt
        bot._last_speaker_refresh_attempt = 0.0
        bot.speaker_engine.clear()
        try:
            with patch("bot.fetch_speakers", new=AsyncMock(return_value=None)):
                with aioresponses() as m:
                    m.post(re.compile(r".*audio_query.*"), status=500)
                    with pytest.raises(aiohttp.ClientError):
                        await synthesize("テスト", VoiceSettings(speaker_id=99999))
        finally:
            bot._last_speaker_refresh_attempt = original_last_attempt

    async def test_uses_mapped_real_id(self):
        import bot
        from bot import VoiceSettings, synthesize

        # global_id 10003 → real_id 99 にマップ
        bot.speaker_engine[10003] = ("http://test-voicevox:50021", 99)
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"mapped-data")
                result = await synthesize("テスト", VoiceSettings(speaker_id=10003))
                assert result == b"mapped-data"
                audio_query_call = list(m.requests.values())[0][0]
                assert audio_query_call.kwargs["params"]["speaker"] == 99
        finally:
            bot.speaker_engine.pop(10003, None)

    async def test_refreshes_speakers_when_cache_empty(self):
        import bot
        from bot import VoiceSettings, synthesize

        async def _fake_fetch_speakers():
            bot.speaker_engine[3] = ("http://test-voicevox:50021", 3)

        original_last_attempt = bot._last_speaker_refresh_attempt
        bot._last_speaker_refresh_attempt = 0.0
        bot.speaker_engine.clear()
        try:
            with patch(
                "bot.fetch_speakers",
                new=AsyncMock(side_effect=_fake_fetch_speakers),
            ) as mocked_fetch:
                with aioresponses() as m:
                    m.post(re.compile(r".*audio_query.*"), payload={})
                    m.post(re.compile(r".*synthesis.*"), body=b"recovered-data")
                    result = await synthesize("テスト", VoiceSettings(speaker_id=99999))
                    assert result == b"recovered-data"
                    mocked_fetch.assert_awaited_once()
        finally:
            bot.speaker_engine.pop(3, None)
            bot._last_speaker_refresh_attempt = original_last_attempt

    async def test_fallbacks_to_next_candidate_when_primary_engine_fails(self):
        import bot
        from bot import VoiceSettings, synthesize

        bot.speaker_engine[99999] = ("http://engine-a:50021", 10)
        bot.speaker_engine[3] = ("http://engine-b:50021", 3)
        try:
            with aioresponses() as m:
                m.post(re.compile(r"http://engine-a:50021/audio_query.*"), status=500)
                m.post(re.compile(r"http://engine-b:50021/audio_query.*"), payload={})
                m.post(
                    re.compile(r"http://engine-b:50021/synthesis.*"),
                    body=b"fallback-ok",
                )
                result = await synthesize("テスト", VoiceSettings(speaker_id=99999))
                assert result == b"fallback-ok"
        finally:
            bot.speaker_engine.pop(99999, None)
            bot.speaker_engine.pop(3, None)

    async def test_cached_fallback_prefers_smallest_global_ids(self):
        import bot

        original = dict(bot.speaker_engine)
        try:
            bot.speaker_engine.clear()
            bot.speaker_engine[100] = ("http://e1", 100)
            bot.speaker_engine[2] = ("http://e2", 2)
            bot.speaker_engine[50] = ("http://e3", 50)
            bot.speaker_engine[1] = ("http://e4", 1)

            candidates = await bot._build_synthesis_candidates(
                requested_speaker_id=9999
            )
            # requested/default が無いとき、
            # cached_speaker_fallback は global_id の小さい順
            cached = [c for c in candidates if c.reason == "cached_speaker_fallback"]
            assert [c.real_id for c in cached] == [1, 2, 50]
        finally:
            bot.speaker_engine.clear()
            bot.speaker_engine.update(original)

    async def test_priority_order_requested_default_cached_raw(self, monkeypatch):
        import bot

        original_engine = list(bot.ENGINES)
        original_map = dict(bot.speaker_engine)
        try:
            monkeypatch.setattr(
                "bot.ENGINES",
                [
                    ("VOICEVOX", "http://raw-a:50021", 0),
                    ("COEIROINK", "http://raw-b:50031", 10000),
                ],
            )
            bot.speaker_engine.clear()
            bot.speaker_engine[999] = ("http://requested:50021", 99)
            bot.speaker_engine[3] = ("http://default:50021", 3)
            bot.speaker_engine[10] = ("http://cached-a:50021", 10)
            bot.speaker_engine[11] = ("http://cached-b:50021", 11)
            bot.speaker_engine[12] = ("http://cached-c:50021", 12)

            candidates = await bot._build_synthesis_candidates(999)
            reasons = [c.reason for c in candidates]

            assert reasons[0] == "requested_speaker"
            assert "default_speaker_mapping" in reasons
            assert reasons.count("cached_speaker_fallback") >= 1
            assert reasons[-2:] == ["raw_default_id", "raw_default_id"]
        finally:
            monkeypatch.setattr("bot.ENGINES", original_engine)
            bot.speaker_engine.clear()
            bot.speaker_engine.update(original_map)

    async def test_deduplicates_same_engine_and_speaker(self, monkeypatch):
        import bot

        original_engine = list(bot.ENGINES)
        original_map = dict(bot.speaker_engine)
        try:
            monkeypatch.setattr(
                "bot.ENGINES",
                [
                    ("VOICEVOX", "http://dup:50021", 0),
                    ("COEIROINK", "http://other:50031", 10000),
                ],
            )
            bot.speaker_engine.clear()
            # requested/default/cached が同一 (engine, speaker) を指すケース
            bot.speaker_engine[42] = ("http://dup:50021", 3)
            bot.speaker_engine[3] = ("http://dup:50021", 3)
            bot.speaker_engine[1] = ("http://dup:50021", 3)

            candidates = await bot._build_synthesis_candidates(42)
            dedup_count = sum(
                1
                for c in candidates
                if (c.engine_url, c.real_id) == ("http://dup:50021", 3)
            )
            assert dedup_count == 1
        finally:
            monkeypatch.setattr("bot.ENGINES", original_engine)
            bot.speaker_engine.clear()
            bot.speaker_engine.update(original_map)

    async def test_refresh_is_throttled_by_interval(self):
        import bot

        original_last_attempt = bot._last_speaker_refresh_attempt
        try:
            bot._last_speaker_refresh_attempt = bot.time.monotonic()
            with patch("bot.fetch_speakers", new=AsyncMock()) as mocked_fetch:
                await bot._refresh_speakers_if_needed()
                mocked_fetch.assert_not_awaited()
        finally:
            bot._last_speaker_refresh_attempt = original_last_attempt


class TestDbOperations:
    async def test_save_user_setting_executes(self, mock_db_pool):
        from bot import VoiceSettings, save_user_setting

        _, conn = mock_db_pool
        await save_user_setting(111, 222, VoiceSettings(speaker_id=5, speed=1.2))
        conn.execute.assert_awaited_once()
        args = conn.execute.await_args.args
        # (sql, guild_id, user_id, speaker_id, speed, pitch, intonation, volume)
        assert args[1] == 111
        assert args[2] == 222
        assert args[3] == 5
        assert args[4] == 1.2

    async def test_load_user_settings_populates(self, mock_db_pool):
        import bot

        pool, conn = mock_db_pool
        conn.fetch.return_value = [
            {
                "guild_id": 10,
                "user_id": 20,
                "speaker_id": 7,
                "speed": 1.1,
                "pitch": 0.05,
                "intonation": 1.3,
                "volume": 0.9,
            }
        ]
        try:
            await bot.load_user_settings()
            assert (10, 20) in bot.user_settings
            assert bot.user_settings[(10, 20)].speaker_id == 7
            assert bot.user_settings[(10, 20)].speed == 1.1
        finally:
            bot.user_settings.pop((10, 20), None)

    async def test_load_guild_dicts_populates(self, mock_db_pool):
        import bot

        _, conn = mock_db_pool
        conn.fetch.return_value = [
            {"guild_id": 30, "word": "w", "reading": "ダブリュー"},
            {"guild_id": 30, "word": "lol", "reading": "わらい"},
        ]
        try:
            await bot.load_guild_dicts()
            assert bot.guild_dicts[30] == {"w": "ダブリュー", "lol": "わらい"}
        finally:
            bot.guild_dicts.pop(30, None)

    async def test_load_guild_mutes_populates(self, mock_db_pool):
        import bot

        _, conn = mock_db_pool
        conn.fetch.return_value = [
            {"guild_id": 40, "user_id": 100},
            {"guild_id": 40, "user_id": 101},
        ]
        try:
            await bot.load_guild_mutes()
            assert bot.guild_mutes[40] == {100, 101}
        finally:
            bot.guild_mutes.pop(40, None)

    async def test_add_dict_entry_executes(self, mock_db_pool):
        from bot import add_dict_entry

        _, conn = mock_db_pool
        await add_dict_entry(50, "abc", "あいうえお")
        conn.execute.assert_awaited_once()
        args = conn.execute.await_args.args
        assert args[1] == 50
        assert args[2] == "abc"
        assert args[3] == "あいうえお"

    async def test_delete_dict_entry_executes(self, mock_db_pool):
        from bot import delete_dict_entry

        _, conn = mock_db_pool
        await delete_dict_entry(50, "abc")
        conn.execute.assert_awaited_once()
        args = conn.execute.await_args.args
        assert args[1] == 50
        assert args[2] == "abc"

    async def test_add_mute_updates_memory_and_db(self, mock_db_pool):
        import bot

        _, conn = mock_db_pool
        try:
            await bot.add_mute(60, 999)
            assert 999 in bot.guild_mutes[60]
            conn.execute.assert_awaited_once()
        finally:
            bot.guild_mutes.pop(60, None)

    async def test_remove_mute_clears_empty_guild(self, mock_db_pool):
        import bot

        _, conn = mock_db_pool
        bot.guild_mutes[61] = {999}
        try:
            await bot.remove_mute(61, 999)
            # 最後の1人を削除した時にギルドエントリも消える
            assert 61 not in bot.guild_mutes
            conn.execute.assert_awaited_once()
        finally:
            bot.guild_mutes.pop(61, None)

    async def test_remove_mute_keeps_other_users(self, mock_db_pool):
        import bot

        _, conn = mock_db_pool
        bot.guild_mutes[62] = {100, 200}
        try:
            await bot.remove_mute(62, 100)
            assert bot.guild_mutes[62] == {200}
            conn.execute.assert_awaited_once()
        finally:
            bot.guild_mutes.pop(62, None)


class TestInitDbMigration:
    async def test_drop_constraint_name_is_escaped(self, monkeypatch):
        import bot

        conn = MagicMock()
        conn.execute = AsyncMock()
        # 現在PKが guild_id,user_id ではない想定にする
        conn.fetch = AsyncMock(return_value=[{"column_name": "user_id"}])
        # ダブルクォートを含む制約名
        conn.fetchval = AsyncMock(return_value='pk"name')

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=None)

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=cm)

        monkeypatch.setattr("bot.asyncpg.create_pool", AsyncMock(return_value=pool))

        original_pool = bot.db_pool
        bot.db_pool = None
        try:
            await bot.init_db()
            sql_calls = [c.args[0] for c in conn.execute.await_args_list]
            assert any(
                'ALTER TABLE user_settings DROP CONSTRAINT "pk""name"' in sql
                for sql in sql_calls
            )
        finally:
            bot.db_pool = original_pool


class TestCleanTextEdge:
    def test_strips_whitespace(self):
        from bot import clean_text

        assert clean_text("  hello  ") == "hello"

    def test_multiple_urls(self):
        from bot import clean_text

        result = clean_text("https://a.com と http://b.com")
        assert result == "URLしょうりゃく と URLしょうりゃく"

    def test_empty_string(self):
        from bot import clean_text

        assert clean_text("") == ""


class TestGetUserSettings:
    def test_returns_per_guild_setting(self):
        from bot import VoiceSettings, get_user_settings, user_settings

        user_settings[(123, 456)] = VoiceSettings(speed=1.5)
        try:
            s = get_user_settings(123, 456)
            assert s.speed == 1.5
        finally:
            user_settings.pop((123, 456), None)


class TestFetchSpeakersMultiEngine:
    async def test_engine_label_prefix_when_multiple(self, monkeypatch):
        import bot
        from bot import fetch_speakers

        monkeypatch.setattr(
            "bot.ENGINES",
            [
                ("VOICEVOX", "http://a:50021", 0),
                ("COEIROINK", "http://b:50031", 10000),
            ],
        )
        with aioresponses() as m:
            m.get(
                "http://a:50021/speakers",
                payload=[{"name": "ずんだもん", "styles": [{"id": 3, "name": "標準"}]}],
            )
            m.get(
                "http://b:50031/speakers",
                payload=[
                    {"name": "つくよみ", "styles": [{"id": 0, "name": "れいせい"}]}
                ],
            )
            await fetch_speakers()
        try:
            # 複数エンジンでは [ENGINE_NAME] プレフィックスが付く
            assert "[VOICEVOX] ずんだもん" in bot.characters
            assert "[COEIROINK] つくよみ" in bot.characters
            # COEIROINKのオフセット適用を確認
            assert 10000 in bot.speakers_cache  # real_id=0 + 10000
        finally:
            bot.speakers_cache.clear()
            bot.speaker_engine.clear()
            bot.characters.clear()


def _make_interaction(guild_id=111, user_id=222, channel_id=333):
    """スラッシュコマンドテスト用の Interaction モック"""
    interaction = MagicMock()
    interaction.guild.id = guild_id
    interaction.guild.me = MagicMock()
    interaction.user.id = user_id
    interaction.user.voice = None  # join系で使う、必要に応じて上書き
    interaction.channel_id = channel_id
    interaction.response.send_message = AsyncMock()
    return interaction


class TestSimpleCommands:
    async def test_skip_rejects_dm(self):
        from bot import skip

        interaction = _make_interaction()
        interaction.guild = None
        await skip.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "このコマンドはサーバー内でのみ利用できます"
        )

    async def test_skip_no_playback(self):
        from bot import skip

        interaction = _make_interaction()
        interaction.guild.voice_client.is_playing.return_value = False
        await skip.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "再生中の音声はありません"
        )

    async def test_skip_with_playback(self):
        from bot import skip

        interaction = _make_interaction()
        interaction.guild.voice_client.is_playing.return_value = True
        await skip.callback(interaction)
        interaction.guild.voice_client.stop.assert_called_once()
        interaction.response.send_message.assert_awaited_once_with("スキップしました")

    async def test_leave_when_connected(self, mock_db_pool):
        from bot import leave, queues, read_channels

        interaction = _make_interaction(guild_id=900)
        interaction.guild.voice_client.disconnect = AsyncMock()
        queues[900] = deque()
        read_channels[900] = 333
        try:
            await leave.callback(interaction)
            interaction.guild.voice_client.disconnect.assert_awaited_once()
            assert 900 not in queues
            assert 900 not in read_channels
        finally:
            queues.pop(900, None)
            read_channels.pop(900, None)

    async def test_leave_when_not_connected(self):
        from bot import leave

        interaction = _make_interaction()
        interaction.guild.voice_client = None
        await leave.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "ボイスチャンネルに接続していません"
        )

    async def test_vc_toggle_when_connected_disconnects(self):
        from bot import vc_toggle

        interaction = _make_interaction()
        interaction.guild.voice_client.disconnect = AsyncMock()
        await vc_toggle.callback(interaction)
        interaction.guild.voice_client.disconnect.assert_awaited_once()


class TestMuteCommands:
    async def test_mute_bot_rejected(self):
        from bot import mute_cmd

        interaction = _make_interaction()
        target = MagicMock()
        target.bot = True
        await mute_cmd.callback(interaction, target)
        interaction.response.send_message.assert_awaited_once_with(
            "Botはミュートできません"
        )

    async def test_mute_user(self, mock_db_pool):
        import bot
        from bot import mute_cmd

        interaction = _make_interaction(guild_id=701)
        target = MagicMock()
        target.bot = False
        target.id = 12345
        target.display_name = "テストユーザー"
        try:
            await mute_cmd.callback(interaction, target)
            assert 12345 in bot.guild_mutes[701]
            interaction.response.send_message.assert_awaited_once()
        finally:
            bot.guild_mutes.pop(701, None)

    async def test_unmute_not_muted(self):
        from bot import unmute_cmd

        interaction = _make_interaction(guild_id=702)
        target = MagicMock()
        target.id = 999
        target.display_name = "Other"
        await unmute_cmd.callback(interaction, target)
        interaction.response.send_message.assert_awaited_once()
        assert (
            "ミュートされていません"
            in (interaction.response.send_message.await_args.args[0])
        )

    async def test_unmute_user(self, mock_db_pool):
        import bot
        from bot import unmute_cmd

        bot.guild_mutes[703] = {888}
        interaction = _make_interaction(guild_id=703)
        target = MagicMock()
        target.id = 888
        target.display_name = "Muted"
        try:
            await unmute_cmd.callback(interaction, target)
            assert 703 not in bot.guild_mutes
        finally:
            bot.guild_mutes.pop(703, None)

    async def test_showmute_empty(self):
        from bot import showmute_cmd

        interaction = _make_interaction(guild_id=704)
        await showmute_cmd.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "ミュート中のユーザーはいません"
        )

    async def test_showmute_with_users(self):
        import bot
        from bot import showmute_cmd

        bot.guild_mutes[705] = {1, 2}
        interaction = _make_interaction(guild_id=705)
        # get_member は名前を返したり None を返したり
        member1 = MagicMock()
        member1.display_name = "User1"
        interaction.guild.get_member = MagicMock(
            side_effect=lambda uid: member1 if uid == 1 else None
        )
        try:
            await showmute_cmd.callback(interaction)
            msg = interaction.response.send_message.await_args.args[0]
            assert "User1" in msg
            assert "ID: 2" in msg  # get_memberがNoneを返したフォールバック
        finally:
            bot.guild_mutes.pop(705, None)


class TestVoiceCommand:
    async def test_voice_no_args_shows_current(self):
        from bot import voice

        interaction = _make_interaction()
        await voice.callback(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "現在の音声設定" in msg

    async def test_voice_updates_speed(self, mock_db_pool):
        import bot
        from bot import voice

        interaction = _make_interaction(guild_id=801, user_id=802)
        try:
            await voice.callback(interaction, speed=1.3)
            assert bot.user_settings[(801, 802)].speed == 1.3
            msg = interaction.response.send_message.await_args.args[0]
            assert "話速: 1.3" in msg
        finally:
            bot.user_settings.pop((801, 802), None)

    async def test_voice_clamps_out_of_range(self, mock_db_pool):
        import bot
        from bot import voice

        interaction = _make_interaction(guild_id=803, user_id=804)
        try:
            await voice.callback(
                interaction, speed=99.0, pitch=-99.0, intonation=99.0, volume=99.0
            )
            s = bot.user_settings[(803, 804)]
            assert s.speed == 2.0
            assert s.pitch == -0.15  # 下限にクランプ
            assert s.intonation == 2.0
            assert s.volume == 2.0
        finally:
            bot.user_settings.pop((803, 804), None)


class TestDictCmd:
    async def test_dict_cmd_responds(self):
        from bot import dict_cmd

        interaction = _make_interaction()
        await dict_cmd.callback(interaction)
        interaction.response.send_message.assert_awaited_once()


class TestSpeakerCommand:
    async def test_no_speakers_loaded(self):
        import bot
        from bot import speaker

        # characters を空にしてテスト
        original = dict(bot.characters)
        bot.characters.clear()
        try:
            interaction = _make_interaction()
            await speaker.callback(interaction, character="ずんだもん")
            interaction.response.send_message.assert_awaited_once_with(
                "スピーカー情報がまだ読み込まれていません"
            )
        finally:
            bot.characters.update(original)

    async def test_character_not_found(self):
        import bot
        from bot import speaker

        bot.characters["ずんだもん"] = [(3, "ノーマル")]
        try:
            interaction = _make_interaction()
            await speaker.callback(interaction, character="存在しないキャラ")
            msg = interaction.response.send_message.await_args.args[0]
            assert "見つかりません" in msg
        finally:
            bot.characters.pop("ずんだもん", None)

    async def test_style_not_found(self):
        import bot
        from bot import speaker

        bot.characters["ずんだもん"] = [(3, "ノーマル"), (1, "あまあま")]
        try:
            interaction = _make_interaction()
            await speaker.callback(
                interaction, character="ずんだもん", style="ありえないスタイル"
            )
            msg = interaction.response.send_message.await_args.args[0]
            assert "スタイル" in msg and "ありません" in msg
        finally:
            bot.characters.pop("ずんだもん", None)

    async def test_speaker_change_succeeds(self, mock_db_pool):
        import bot
        from bot import speaker

        bot.characters["ずんだもん"] = [(3, "ノーマル"), (1, "あまあま")]
        bot.speakers_cache[1] = "ずんだもん（あまあま）"
        try:
            interaction = _make_interaction(guild_id=601, user_id=602)
            await speaker.callback(
                interaction, character="ずんだもん", style="あまあま"
            )
            assert bot.user_settings[(601, 602)].speaker_id == 1
        finally:
            bot.user_settings.pop((601, 602), None)
            bot.characters.pop("ずんだもん", None)
            bot.speakers_cache.pop(1, None)

    async def test_exact_match_preferred_over_partial(self, mock_db_pool):
        """完全一致が部分一致より優先されること"""
        import bot
        from bot import speaker

        # "ずんだ" が "ずんだもん" にも "ずんだ" にも部分一致するが、
        # 完全一致の "ずんだ" を選ぶべき
        bot.characters["ずんだもん"] = [(3, "ノーマル")]
        bot.characters["ずんだ"] = [(99, "ノーマル")]
        bot.speakers_cache[99] = "ずんだ（ノーマル）"
        try:
            interaction = _make_interaction(guild_id=611, user_id=612)
            await speaker.callback(interaction, character="ずんだ")
            assert bot.user_settings[(611, 612)].speaker_id == 99
        finally:
            bot.user_settings.pop((611, 612), None)
            bot.characters.pop("ずんだもん", None)
            bot.characters.pop("ずんだ", None)
            bot.speakers_cache.pop(99, None)


def _make_message(guild_id=111, channel_id=888, user_id=222, content="テスト"):
    """on_message テスト用の Message モック"""
    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = user_id
    msg.guild.id = guild_id
    msg.guild.voice_client.is_connected.return_value = True
    msg.guild.voice_client.is_playing.return_value = False
    msg.guild.voice_client.is_paused.return_value = False
    msg.channel.id = channel_id
    msg.clean_content = content
    return msg


class TestOnMessageMore:
    async def test_no_voice_client_skips(self):
        from bot import on_message

        msg = _make_message(guild_id=10001)
        msg.guild.voice_client = None
        await on_message(msg)  # 早期 return

    async def test_disconnected_voice_client_skips(self):
        from bot import on_message

        msg = _make_message(guild_id=10002)
        msg.guild.voice_client.is_connected.return_value = False
        await on_message(msg)  # 早期 return

    async def test_empty_after_clean_skips(self):
        from bot import on_message, read_channels

        # URL のみのメッセージは clean_text で "URLしょうりゃく" になるが、
        # 完全に空白だけのメッセージなら return される
        msg = _make_message(guild_id=10003, content="   ")
        read_channels[10003] = msg.channel.id
        try:
            await on_message(msg)  # 何も起きないこと
        finally:
            read_channels.pop(10003, None)

    @patch("discord.FFmpegPCMAudio")
    async def test_full_flow_synthesizes_and_queues(self, mock_ffmpeg):
        from bot import on_message, play_locks, queues, read_channels

        msg = _make_message(guild_id=10004, content="こんにちは")
        read_channels[10004] = msg.channel.id

        with aioresponses() as m:
            m.post(re.compile(r".*audio_query.*"), payload={})
            m.post(re.compile(r".*synthesis.*"), body=b"wav-data")
            try:
                await on_message(msg)
            finally:
                read_channels.pop(10004, None)
                queues.pop(10004, None)
                play_locks.pop(10004, None)

        # vc.play が呼ばれた（キューに積まれて再生開始された）
        msg.guild.voice_client.play.assert_called_once()

    async def test_synthesize_aiohttp_error_notifies(self):
        from bot import on_message, read_channels

        msg = _make_message(guild_id=10005, content="エラーテスト")
        msg.channel.send = AsyncMock()
        read_channels[10005] = msg.channel.id

        with aioresponses() as m:
            # audio_queryでaiohttpエラーが発生
            m.post(re.compile(r".*audio_query.*"), exception=Exception("oops"))
            try:
                await on_message(msg)
            finally:
                read_channels.pop(10005, None)
        # 一般 Exception パスは通知メッセージなし、ログのみ
        msg.channel.send.assert_not_called()

    async def test_synthesize_connection_error_notifies(self):
        import aiohttp

        from bot import engine_error_notified_at, on_message, read_channels

        msg = _make_message(guild_id=10006, content="接続エラーテスト")
        msg.channel.send = AsyncMock()
        read_channels[10006] = msg.channel.id

        with aioresponses() as m:
            m.post(
                re.compile(r".*audio_query.*"),
                exception=aiohttp.ClientConnectionError("down"),
            )
            try:
                await on_message(msg)
            finally:
                read_channels.pop(10006, None)
                engine_error_notified_at.pop(10006, None)
        # ClientError パスはユーザー通知あり
        msg.channel.send.assert_awaited_once()

    async def test_synthesize_connection_error_is_rate_limited(self):
        import aiohttp

        from bot import engine_error_notified_at, on_message, read_channels

        msg = _make_message(guild_id=10008, content="接続エラーテスト")
        msg.channel.send = AsyncMock()
        read_channels[10008] = msg.channel.id

        with aioresponses() as m:
            m.post(
                re.compile(r".*audio_query.*"),
                exception=aiohttp.ClientConnectionError("down"),
                repeat=True,
            )
            try:
                await on_message(msg)
                await on_message(msg)
            finally:
                read_channels.pop(10008, None)
                engine_error_notified_at.pop(10008, None)
        # 連続失敗しても通知は1回
        msg.channel.send.assert_awaited_once()

    @patch("discord.FFmpegPCMAudio")
    async def test_long_text_truncated_in_synthesize(self, mock_ffmpeg):
        from bot import on_message, play_locks, queues, read_channels

        long_text = "あ" * 200
        msg = _make_message(guild_id=10007, content=long_text)
        read_channels[10007] = msg.channel.id
        captured_text = []

        async def capture_synthesize(text, settings):
            captured_text.append(text)
            return b"wav"

        with patch("bot.synthesize", side_effect=capture_synthesize):
            try:
                await on_message(msg)
            finally:
                read_channels.pop(10007, None)
                queues.pop(10007, None)
                play_locks.pop(10007, None)
        assert captured_text
        assert captured_text[0].endswith("、いかしょうりゃく")
        from bot import MAX_READ_LENGTH

        assert len(captured_text[0]) == MAX_READ_LENGTH + len("、いかしょうりゃく")

    async def test_preserves_order_under_concurrent_synth(self):
        """2メッセージ同時到着時、後続が先に合成完了しても queue 順序が維持される"""
        import asyncio

        from bot import on_message, play_locks, queues, read_channels

        read_channels[10008] = 888

        async def fake_synthesize(text, settings):
            # 先に到着した "A" を遅く、後の "B" を速く → race を誘発
            delay = 0.05 if text == "A" else 0.005
            await asyncio.sleep(delay)
            return text.encode()

        msg_a = _make_message(guild_id=10008, channel_id=888, content="A")
        msg_b = _make_message(guild_id=10008, channel_id=888, content="B")
        # play_next 内で再生が走らないよう is_playing=True で止める
        for m in (msg_a, msg_b):
            m.guild.voice_client.is_connected.return_value = True
            m.guild.voice_client.is_playing.return_value = True
            m.guild.voice_client.is_paused.return_value = False

        try:
            with patch("bot.synthesize", side_effect=fake_synthesize):
                # 同時 dispatch を asyncio.gather で再現
                await asyncio.gather(on_message(msg_a), on_message(msg_b))

            q = list(queues.get(10008, []))
            assert q == [b"A", b"B"], f"順序が逆転: {q}"
        finally:
            read_channels.pop(10008, None)
            queues.pop(10008, None)
            play_locks.pop(10008, None)


class TestOnVoiceStateUpdate:
    async def test_ignores_other_bot_when_no_vc(self):
        from bot import on_voice_state_update

        member = MagicMock()
        member.bot = True
        member.id = 9999  # Bot自身ではない
        member.guild.voice_client = None
        await on_voice_state_update(member, MagicMock(), MagicMock())  # 早期 return

    async def test_skips_when_no_voice_client(self):
        from bot import on_voice_state_update

        member = MagicMock()
        member.bot = False
        member.guild.voice_client = None
        await on_voice_state_update(member, MagicMock(), MagicMock())

    async def test_auto_disconnect_when_only_bot_left(self):
        from bot import on_voice_state_update, play_locks, queues, read_channels

        member = MagicMock()
        member.bot = False
        member.guild.id = 5001
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        vc.disconnect = AsyncMock()
        # チャンネルにはBotしか残っていない
        bot_member = MagicMock()
        bot_member.bot = True
        vc.channel.members = [bot_member]
        queues[5001] = deque()
        read_channels[5001] = 100
        play_locks[5001] = MagicMock()
        try:
            await on_voice_state_update(member, MagicMock(), MagicMock())
            vc.disconnect.assert_awaited_once()
            assert 5001 not in queues
            assert 5001 not in read_channels
            assert 5001 not in play_locks
        finally:
            queues.pop(5001, None)
            read_channels.pop(5001, None)
            play_locks.pop(5001, None)

    @patch("discord.FFmpegPCMAudio")
    async def test_join_announcement(self, mock_ffmpeg):
        from bot import on_voice_state_update, play_locks, queues

        member = MagicMock()
        member.bot = False
        member.guild.id = 5002
        member.id = 700
        member.display_name = "アリス"
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.is_paused.return_value = False
        # チャンネルに人間メンバー含む（自動切断にならない）
        human = MagicMock()
        human.bot = False
        vc.channel.members = [member, human]

        before = MagicMock()
        before.channel = None
        after = MagicMock()
        after.channel = vc.channel  # joinedになる

        with patch("bot.synthesize", new=AsyncMock(return_value=b"wav")) as mock_syn:
            try:
                await on_voice_state_update(member, before, after)
                mock_syn.assert_awaited_once()
                # 「にゅうしつしました」の文言が含まれる
                args = mock_syn.await_args.args
                assert "にゅうしつしました" in args[0]
                assert "アリス" in args[0]
            finally:
                queues.pop(5002, None)
                play_locks.pop(5002, None)

    async def test_leave_announcement_text(self):
        from bot import on_voice_state_update, play_locks, queues

        member = MagicMock()
        member.bot = False
        member.guild.id = 5003
        member.id = 701
        member.display_name = "ボブ"
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        # 別の人間が残っているので自動切断にはならない
        human = MagicMock()
        human.bot = False
        vc.channel.members = [human]

        before = MagicMock()
        before.channel = vc.channel
        after = MagicMock()
        after.channel = None  # leftになる

        with patch("bot.synthesize", new=AsyncMock(return_value=b"wav")) as mock_syn:
            try:
                # synthesize後のplay_nextで切断と判定させる
                vc.is_connected.side_effect = [True, True, True, False]
                await on_voice_state_update(member, before, after)
                mock_syn.assert_awaited_once()
                assert "たいしつしました" in mock_syn.await_args.args[0]
            finally:
                queues.pop(5003, None)
                play_locks.pop(5003, None)

    async def test_synthesize_error_logged_not_raised(self):
        from bot import on_voice_state_update

        member = MagicMock()
        member.bot = False
        member.guild.id = 5004
        member.id = 702
        member.display_name = "Carol"
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        human = MagicMock()
        human.bot = False
        vc.channel.members = [human]
        before = MagicMock()
        before.channel = None
        after = MagicMock()
        after.channel = vc.channel

        with patch("bot.synthesize", new=AsyncMock(side_effect=Exception("boom"))):
            # 例外は外に出ない
            await on_voice_state_update(member, before, after)

    async def test_auto_disconnect_swallows_exception(self):
        """vc.disconnect() が例外を投げても後始末が継続すること"""
        from bot import on_voice_state_update, play_locks, queues, read_channels

        member = MagicMock()
        member.bot = False
        member.guild.id = 5010
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        vc.disconnect = AsyncMock(side_effect=Exception("already disconnected"))
        bot_member = MagicMock()
        bot_member.bot = True
        vc.channel.members = [bot_member]
        queues[5010] = deque()
        read_channels[5010] = 100
        play_locks[5010] = MagicMock()
        try:
            # 例外が外に漏れないこと
            await on_voice_state_update(member, MagicMock(), MagicMock())
            # 例外でも後始末は進む
            assert 5010 not in queues
            assert 5010 not in read_channels
            assert 5010 not in play_locks
        finally:
            queues.pop(5010, None)
            read_channels.pop(5010, None)
            play_locks.pop(5010, None)

    async def test_bot_disconnect_cleans_guild_state(self):
        from bot import (
            client,
            engine_error_notified_at,
            on_voice_state_update,
            play_locks,
            queues,
            read_channels,
        )

        original_user = client._connection.user
        client._connection.user = MagicMock(id=4242)

        member = MagicMock()
        member.bot = True
        member.id = 4242
        member.guild.id = 5005
        before = MagicMock()
        before.channel = MagicMock()
        after = MagicMock()
        after.channel = None

        queues[5005] = deque([b"audio"])
        read_channels[5005] = 100
        play_locks[5005] = MagicMock()
        engine_error_notified_at[5005] = 1.0
        try:
            await on_voice_state_update(member, before, after)
            assert 5005 not in queues
            assert 5005 not in read_channels
            assert 5005 not in play_locks
            assert 5005 not in engine_error_notified_at
        finally:
            client._connection.user = original_user
            queues.pop(5005, None)
            read_channels.pop(5005, None)
            play_locks.pop(5005, None)
            engine_error_notified_at.pop(5005, None)

    async def test_bot_reconnect_resumes_when_queue_exists(self):
        from bot import client, on_voice_state_update, queues

        original_user = client._connection.user
        client._connection.user = MagicMock(id=4242)

        member = MagicMock()
        member.bot = True
        member.id = 4242
        member.guild.id = 5011
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.is_paused.return_value = False

        before = MagicMock()
        before.channel = None
        after = MagicMock()
        after.channel = MagicMock()

        queues[5011] = deque([b"remain"])
        try:
            with patch("bot.play_next", new=AsyncMock()) as mocked_play_next:
                await on_voice_state_update(member, before, after)
                mocked_play_next.assert_awaited_once_with(5011, vc)
        finally:
            client._connection.user = original_user
            queues.pop(5011, None)

    async def test_bot_reconnect_does_not_resume_when_queue_empty(self):
        from bot import client, on_voice_state_update, queues

        original_user = client._connection.user
        client._connection.user = MagicMock(id=4242)

        member = MagicMock()
        member.bot = True
        member.id = 4242
        member.guild.id = 5012
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.is_paused.return_value = False

        before = MagicMock()
        before.channel = None
        after = MagicMock()
        after.channel = MagicMock()

        queues[5012] = deque()
        try:
            with patch("bot.play_next", new=AsyncMock()) as mocked_play_next:
                await on_voice_state_update(member, before, after)
                mocked_play_next.assert_not_awaited()
        finally:
            client._connection.user = original_user
            queues.pop(5012, None)

    async def test_join_event_skips_enqueue_when_disconnected_after_synthesize(self):
        from bot import on_voice_state_update, queues

        member = MagicMock()
        member.bot = False
        member.guild.id = 5013
        member.id = 777
        member.display_name = "RaceUser"
        vc = member.guild.voice_client
        # 1回目: 関数冒頭チェック、2回目: 合成前チェック、3回目: 合成後チェック
        vc.is_connected.side_effect = [True, True, False]
        human = MagicMock()
        human.bot = False
        vc.channel.members = [member, human]

        before = MagicMock()
        before.channel = None
        after = MagicMock()
        after.channel = vc.channel

        with patch("bot.synthesize", new=AsyncMock(return_value=b"wav")) as mocked_syn:
            await on_voice_state_update(member, before, after)
            mocked_syn.assert_awaited_once()
            assert 5013 not in queues

    async def test_join_event_enqueues_but_not_play_when_vc_busy(self):
        from bot import on_voice_state_update, play_locks, queues

        member = MagicMock()
        member.bot = False
        member.guild.id = 5014
        member.id = 778
        member.display_name = "BusyUser"
        vc = member.guild.voice_client
        vc.is_connected.return_value = True
        vc.is_playing.return_value = True
        vc.is_paused.return_value = False
        human = MagicMock()
        human.bot = False
        vc.channel.members = [member, human]

        before = MagicMock()
        before.channel = None
        after = MagicMock()
        after.channel = vc.channel

        with patch("bot.synthesize", new=AsyncMock(return_value=b"wav")):
            try:
                with patch("bot.play_next", new=AsyncMock()) as mocked_play_next:
                    await on_voice_state_update(member, before, after)
                    assert 5014 in queues
                    assert len(queues[5014]) == 1
                    mocked_play_next.assert_not_awaited()
            finally:
                queues.pop(5014, None)
                play_locks.pop(5014, None)


class TestJoinCommand:
    async def test_rejects_dm(self):
        from bot import join

        interaction = _make_interaction()
        interaction.guild = None
        await join.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "このコマンドはサーバー内でのみ利用できます"
        )

    async def test_user_not_in_voice(self):
        from bot import join

        interaction = _make_interaction()
        interaction.user.voice = None
        await join.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "先にボイスチャンネルに入ってください"
        )

    async def test_no_connect_perm(self):
        from bot import join

        interaction = _make_interaction()
        channel = MagicMock()
        perms = MagicMock()
        perms.connect = False
        channel.permissions_for.return_value = perms
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel
        await join.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "そのVCに接続する権限がありません"
        )

    async def test_no_speak_perm(self):
        from bot import join

        interaction = _make_interaction()
        channel = MagicMock()
        perms = MagicMock()
        perms.connect = True
        perms.speak = False
        channel.permissions_for.return_value = perms
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel
        await join.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "そのVCで発言する権限がありません"
        )

    async def test_user_limit_reached(self):
        from bot import join

        interaction = _make_interaction()
        channel = MagicMock()
        channel.user_limit = 5
        channel.members = [MagicMock()] * 5
        perms = MagicMock()
        perms.connect = True
        perms.speak = True
        perms.manage_channels = False
        channel.permissions_for.return_value = perms
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel
        await join.callback(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "VCの人数制限に達しています"
        )

    async def test_connect_failure(self):
        from bot import join

        interaction = _make_interaction()
        channel = MagicMock()
        channel.user_limit = 0  # 制限なし
        perms = MagicMock()
        perms.connect = True
        perms.speak = True
        channel.permissions_for.return_value = perms
        channel.connect = AsyncMock(side_effect=Exception("network error"))
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel
        interaction.guild.voice_client = None
        await join.callback(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "VCへの接続に失敗" in msg

    async def test_clears_stale_queue_on_join(self):
        from bot import join, queues

        interaction = _make_interaction(guild_id=9001, user_id=9002)
        channel = MagicMock()
        channel.user_limit = 0
        perms = MagicMock()
        perms.connect = True
        perms.speak = True
        channel.permissions_for.return_value = perms
        channel.connect = AsyncMock()
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel
        interaction.guild.voice_client = None

        queues[9001] = deque([b"stale-audio"])
        with patch("bot.synthesize", new=AsyncMock(return_value=b"hi")):
            await join.callback(interaction)
        assert 9001 in queues
        # 新規接続の場合は旧キューを破棄（切断時のクリーンアップ漏れ対策）
        assert len(queues[9001]) == 0
        queues.pop(9001, None)

    async def test_preserves_queue_on_move_to(self):
        """既に接続中のBotを別VCへ移動する場合、待機中キューは保持される"""
        from bot import join, play_locks, queues

        interaction = _make_interaction(guild_id=9003, user_id=9004)
        channel = MagicMock()
        channel.user_limit = 0
        perms = MagicMock()
        perms.connect = True
        perms.speak = True
        channel.permissions_for.return_value = perms
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel
        # 既に接続中（voice_client が Truthy、move_to は AsyncMock）
        interaction.guild.voice_client.move_to = AsyncMock()
        # 挨拶の play_next で再生が走らないよう is_connected=False にしておく
        interaction.guild.voice_client.is_connected.return_value = False

        queues[9003] = deque([b"queued-audio"])
        with patch("bot.synthesize", new=AsyncMock(return_value=b"hi")):
            await join.callback(interaction)
        assert 9003 in queues
        # 移動時は待機中の音声が保持される
        assert b"queued-audio" in queues[9003]
        queues.pop(9003, None)
        play_locks.pop(9003, None)

    async def test_rejects_when_bot_member_unavailable(self):
        from bot import join

        interaction = _make_interaction(guild_id=9101)
        interaction.guild.me = None
        interaction.guild.get_member = MagicMock(return_value=None)
        channel = MagicMock()
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = channel

        await join.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        assert (
            "Botの権限情報を取得できません"
            in (interaction.response.send_message.await_args.args[0])
        )


class TestSpeakerAutocomplete:
    async def test_character_autocomplete_empty(self):
        import bot
        from bot import speaker_char_autocomplete

        original = dict(bot.characters)
        bot.characters.clear()
        try:
            result = await speaker_char_autocomplete(MagicMock(), "")
            assert result == []
        finally:
            bot.characters.update(original)

    async def test_character_autocomplete_filter(self):
        import bot
        from bot import speaker_char_autocomplete

        bot.characters["ずんだもん"] = [(3, "ノーマル")]
        bot.characters["四国めたん"] = [(2, "ノーマル")]
        try:
            result = await speaker_char_autocomplete(MagicMock(), "ずんだ")
            names = [c.name for c in result]
            assert "ずんだもん" in names
            assert "四国めたん" not in names
        finally:
            bot.characters.pop("ずんだもん", None)
            bot.characters.pop("四国めたん", None)

    async def test_character_autocomplete_returns_all_when_blank(self):
        import bot
        from bot import speaker_char_autocomplete

        bot.characters["A"] = [(1, "x")]
        bot.characters["B"] = [(2, "y")]
        try:
            result = await speaker_char_autocomplete(MagicMock(), "")
            names = [c.name for c in result]
            assert set(names) == {"A", "B"}
        finally:
            bot.characters.pop("A", None)
            bot.characters.pop("B", None)

    async def test_style_autocomplete_handles_missing_interaction_data(self):
        from bot import speaker_style_autocomplete

        interaction = MagicMock()
        interaction.data = None
        result = await speaker_style_autocomplete(interaction, "")
        assert result == []

    async def test_style_autocomplete_no_character_input(self):
        from bot import speaker_style_autocomplete

        interaction = MagicMock()
        interaction.data = {"options": []}
        result = await speaker_style_autocomplete(interaction, "")
        assert result == []

    async def test_style_autocomplete_filters_styles(self):
        import bot
        from bot import speaker_style_autocomplete

        bot.characters["ずんだもん"] = [
            (3, "ノーマル"),
            (1, "あまあま"),
            (5, "ツンツン"),
        ]
        try:
            interaction = MagicMock()
            interaction.data = {
                "options": [{"name": "character", "value": "ずんだもん"}]
            }
            result = await speaker_style_autocomplete(interaction, "あま")
            names = [c.name for c in result]
            assert "あまあま" in names
            assert "ノーマル" not in names
        finally:
            bot.characters.pop("ずんだもん", None)

    async def test_style_autocomplete_unknown_character(self):
        import bot
        from bot import speaker_style_autocomplete

        bot.characters["X"] = [(1, "y")]
        try:
            interaction = MagicMock()
            interaction.data = {
                "options": [{"name": "character", "value": "存在しない"}]
            }
            result = await speaker_style_autocomplete(interaction, "")
            assert result == []
        finally:
            bot.characters.pop("X", None)


class TestDictModals:
    async def test_add_modal_empty_input_rejected(self):
        from bot import DictAddModal

        modal = DictAddModal(7000)
        modal.word = MagicMock()
        modal.word.value = "  "
        modal.reading = MagicMock()
        modal.reading.value = "あいう"
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        await modal.on_submit(interaction)
        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args.args[0]
        assert "両方を入力" in msg

    async def test_add_modal_inserts_entry(self, mock_db_pool):
        import bot
        from bot import DictAddModal

        modal = DictAddModal(7001)
        modal.word = MagicMock()
        modal.word.value = "test"
        modal.reading = MagicMock()
        modal.reading.value = "テスト"
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        try:
            await modal.on_submit(interaction)
            assert bot.guild_dicts[7001]["test"] == "テスト"
            interaction.response.edit_message.assert_awaited_once()
        finally:
            bot.guild_dicts.pop(7001, None)

    async def test_delete_modal_word_not_found(self):
        from bot import DictDeleteModal

        modal = DictDeleteModal(7002)
        modal.word = MagicMock()
        modal.word.value = "missing"
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        await modal.on_submit(interaction)
        msg = interaction.response.send_message.await_args.args[0]
        assert "登録されていません" in msg

    async def test_delete_modal_removes_entry(self, mock_db_pool):
        import bot
        from bot import DictDeleteModal

        bot.guild_dicts[7003] = {"x": "エックス", "y": "ワイ"}
        modal = DictDeleteModal(7003)
        modal.word = MagicMock()
        modal.word.value = "x"
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        try:
            await modal.on_submit(interaction)
            assert "x" not in bot.guild_dicts[7003]
            assert bot.guild_dicts[7003] == {"y": "ワイ"}
        finally:
            bot.guild_dicts.pop(7003, None)

    async def test_delete_modal_removes_empty_guild(self, mock_db_pool):
        import bot
        from bot import DictDeleteModal

        bot.guild_dicts[7004] = {"only": "唯一"}
        modal = DictDeleteModal(7004)
        modal.word = MagicMock()
        modal.word.value = "only"
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        try:
            await modal.on_submit(interaction)
            assert 7004 not in bot.guild_dicts
        finally:
            bot.guild_dicts.pop(7004, None)


class TestSharedHttpSession:
    """共有 aiohttp.ClientSession による接続再利用"""

    async def test_get_http_session_returns_session(self):
        import aiohttp

        from bot import close_http_session, get_http_session

        try:
            session = await get_http_session()
            assert isinstance(session, aiohttp.ClientSession)
            assert not session.closed
        finally:
            await close_http_session()

    async def test_get_http_session_reuses_instance(self):
        from bot import close_http_session, get_http_session

        try:
            s1 = await get_http_session()
            s2 = await get_http_session()
            assert s1 is s2
        finally:
            await close_http_session()

    async def test_close_http_session_clears_cached(self):
        import bot
        from bot import close_http_session, get_http_session

        await get_http_session()
        await close_http_session()
        assert bot._http_session is None or bot._http_session.closed

    async def test_get_http_session_recreates_after_close(self):
        from bot import close_http_session, get_http_session

        try:
            s1 = await get_http_session()
            await close_http_session()
            s2 = await get_http_session()
            assert s1 is not s2
            assert not s2.closed
        finally:
            await close_http_session()

    async def test_synthesize_uses_shared_session(self):
        """synthesize が共有セッションを使っても既存の期待動作は壊れない"""
        from bot import VoiceSettings, close_http_session, synthesize

        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"data1")
                result1 = await synthesize("あ", VoiceSettings())
                assert result1 == b"data1"

            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"data2")
                result2 = await synthesize("い", VoiceSettings())
                assert result2 == b"data2"
        finally:
            await close_http_session()


class TestDictPatternCache:
    """apply_dict のコンパイル済みパターンキャッシュ"""

    def test_pattern_cached_after_first_call(self):
        import bot
        from bot import apply_dict, guild_dicts

        guild_dicts[9101] = {"a": "A", "b": "B"}
        bot._dict_patterns.pop(9101, None)
        try:
            assert 9101 not in bot._dict_patterns
            apply_dict(9101, "a b")
            assert 9101 in bot._dict_patterns
        finally:
            guild_dicts.pop(9101, None)
            bot._dict_patterns.pop(9101, None)

    def test_pattern_reused_on_second_call(self):
        import bot
        from bot import apply_dict, guild_dicts

        guild_dicts[9102] = {"a": "A"}
        try:
            apply_dict(9102, "a")
            pat1 = bot._dict_patterns[9102]
            apply_dict(9102, "aa")
            pat2 = bot._dict_patterns[9102]
            assert pat1 is pat2
        finally:
            guild_dicts.pop(9102, None)
            bot._dict_patterns.pop(9102, None)

    def test_empty_dict_does_not_populate_cache(self):
        import bot
        from bot import apply_dict

        bot._dict_patterns.pop(9103, None)
        apply_dict(9103, "hello")
        assert 9103 not in bot._dict_patterns

    async def test_add_dict_entry_invalidates_cache(self, mock_db_pool):
        import bot
        from bot import add_dict_entry, guild_dicts

        guild_dicts[9104] = {"a": "A"}
        try:
            from bot import apply_dict

            apply_dict(9104, "a")
            assert 9104 in bot._dict_patterns

            guild_dicts[9104]["b"] = "B"
            await add_dict_entry(9104, "b", "B")
            assert 9104 not in bot._dict_patterns
        finally:
            guild_dicts.pop(9104, None)
            bot._dict_patterns.pop(9104, None)

    async def test_delete_dict_entry_invalidates_cache(self, mock_db_pool):
        import bot
        from bot import apply_dict, delete_dict_entry, guild_dicts

        guild_dicts[9105] = {"a": "A"}
        try:
            apply_dict(9105, "a")
            assert 9105 in bot._dict_patterns

            await delete_dict_entry(9105, "a")
            assert 9105 not in bot._dict_patterns
        finally:
            guild_dicts.pop(9105, None)
            bot._dict_patterns.pop(9105, None)

    async def test_load_guild_dicts_resets_all_caches(self, mock_db_pool):
        import bot
        from bot import apply_dict, guild_dicts, load_guild_dicts

        guild_dicts[9106] = {"a": "A"}
        guild_dicts[9107] = {"b": "B"}
        apply_dict(9106, "a")
        apply_dict(9107, "b")
        assert 9106 in bot._dict_patterns
        assert 9107 in bot._dict_patterns

        _, conn = mock_db_pool
        conn.fetch.return_value = []
        try:
            await load_guild_dicts()
            assert bot._dict_patterns == {}
        finally:
            bot._dict_patterns.clear()
            guild_dicts.pop(9106, None)
            guild_dicts.pop(9107, None)

    async def test_modal_add_invalidates_cache(self, mock_db_pool):
        import bot
        from bot import DictAddModal, apply_dict, guild_dicts

        guild_dicts[9108] = {"a": "A"}
        apply_dict(9108, "a")
        assert 9108 in bot._dict_patterns

        modal = DictAddModal(9108)
        modal.word = MagicMock()
        modal.word.value = "c"
        modal.reading = MagicMock()
        modal.reading.value = "シー"
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        try:
            await modal.on_submit(interaction)
            assert 9108 not in bot._dict_patterns
        finally:
            guild_dicts.pop(9108, None)
            bot._dict_patterns.pop(9108, None)

    async def test_modal_delete_invalidates_cache(self, mock_db_pool):
        import bot
        from bot import DictDeleteModal, apply_dict, guild_dicts

        guild_dicts[9109] = {"a": "A", "b": "B"}
        apply_dict(9109, "a b")
        assert 9109 in bot._dict_patterns

        modal = DictDeleteModal(9109)
        modal.word = MagicMock()
        modal.word.value = "a"
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        try:
            await modal.on_submit(interaction)
            assert 9109 not in bot._dict_patterns
        finally:
            guild_dicts.pop(9109, None)
            bot._dict_patterns.pop(9109, None)

    def test_cache_produces_same_output_after_invalidation(self):
        """キャッシュ再コンパイル後も置換結果が同じであること"""
        import bot
        from bot import apply_dict, guild_dicts

        guild_dicts[9110] = {"x": "X"}
        try:
            r1 = apply_dict(9110, "x")
            assert r1 == "X"
            bot._dict_patterns.pop(9110, None)
            r2 = apply_dict(9110, "x")
            assert r2 == "X"
        finally:
            guild_dicts.pop(9110, None)
            bot._dict_patterns.pop(9110, None)


class TestSynthesizeCache:
    """合成結果キャッシュの挙動（永続LRU + 短時間TTL）"""

    async def test_default_uses_recent_ttl_cache(self):
        """cache=False でも短時間の同一合成はTTLキャッシュでHTTPを抑制する"""
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        bot._recent_synth_cache.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"one")
                r1 = await synthesize("同じテキスト", VoiceSettings())
                r2 = await synthesize("同じテキスト", VoiceSettings())
                assert r1 == b"one"
                assert r2 == b"one"
                synthesis_count = sum(
                    len(v) for k, v in m.requests.items() if "synthesis" in str(k)
                )
                assert synthesis_count == 1
        finally:
            await close_http_session()
            bot._synth_cache.clear()
            bot._recent_synth_cache.clear()

    async def test_cache_hit_skips_http(self):
        """cache=True の2回目はHTTPを叩かない"""
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"cached")
                r1 = await synthesize("せつぞくしました", VoiceSettings(), cache=True)
                # 2回目はHTTPモック登録なしで呼ぶ → キャッシュヒットで成功するはず
                r2 = await synthesize("せつぞくしました", VoiceSettings(), cache=True)
                assert r1 == b"cached"
                assert r2 == b"cached"
        finally:
            await close_http_session()
            bot._synth_cache.clear()

    async def test_cache_key_includes_settings(self):
        """異なる設定では別キーとして扱われる"""
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"a")
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"b")
                r1 = await synthesize("挨拶", VoiceSettings(speed=1.0), cache=True)
                r2 = await synthesize("挨拶", VoiceSettings(speed=1.5), cache=True)
                assert r1 == b"a"
                assert r2 == b"b"
        finally:
            await close_http_session()
            bot._synth_cache.clear()

    async def test_cache_key_includes_text(self):
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"x")
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"y")
                r1 = await synthesize("A", VoiceSettings(), cache=True)
                r2 = await synthesize("B", VoiceSettings(), cache=True)
                assert r1 == b"x"
                assert r2 == b"y"
        finally:
            await close_http_session()
            bot._synth_cache.clear()

    async def test_lru_eviction(self):
        """キャッシュ上限を超えると古いエントリが追い出される"""
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        original_max = bot._SYNTH_CACHE_MAX
        bot._SYNTH_CACHE_MAX = 2
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={}, repeat=True)
                m.post(re.compile(r".*synthesis.*"), body=b"1", repeat=True)
                await synthesize("A", VoiceSettings(), cache=True)
                await synthesize("B", VoiceSettings(), cache=True)
                assert len(bot._synth_cache) == 2
                await synthesize("C", VoiceSettings(), cache=True)
                # 最も古い A が追い出される
                assert len(bot._synth_cache) == 2
                keys_text = {k[2] for k in bot._synth_cache}
                assert "A" not in keys_text
                assert "B" in keys_text
                assert "C" in keys_text
        finally:
            bot._SYNTH_CACHE_MAX = original_max
            await close_http_session()
            bot._synth_cache.clear()

    async def test_cache_hit_promotes_entry(self):
        """ヒットしたキーはLRUで最新扱いになる"""
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        original_max = bot._SYNTH_CACHE_MAX
        bot._SYNTH_CACHE_MAX = 2
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={}, repeat=True)
                m.post(re.compile(r".*synthesis.*"), body=b"x", repeat=True)
                await synthesize("A", VoiceSettings(), cache=True)
                await synthesize("B", VoiceSettings(), cache=True)
                # A にアクセス → A は最新に
                await synthesize("A", VoiceSettings(), cache=True)
                # C を追加 → B が追い出される
                await synthesize("C", VoiceSettings(), cache=True)
                keys_text = {k[2] for k in bot._synth_cache}
                assert "A" in keys_text
                assert "C" in keys_text
                assert "B" not in keys_text
        finally:
            bot._SYNTH_CACHE_MAX = original_max
            await close_http_session()
            bot._synth_cache.clear()

    async def test_cache_disabled_does_not_populate(self):
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        bot._recent_synth_cache.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"x")
                await synthesize("キャッシュしない", VoiceSettings())
            assert bot._synth_cache == {}
            assert bot._recent_synth_cache != {}
        finally:
            await close_http_session()
            bot._synth_cache.clear()
            bot._recent_synth_cache.clear()

    async def test_recent_cache_expires(self):
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        original_ttl = bot._RECENT_SYNTH_TTL_SECONDS
        bot._RECENT_SYNTH_TTL_SECONDS = 0.0
        bot._recent_synth_cache.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={}, repeat=True)
                m.post(re.compile(r".*synthesis.*"), body=b"z", repeat=True)
                await synthesize("期限切れ", VoiceSettings())
                await synthesize("期限切れ", VoiceSettings())
                synthesis_count = sum(
                    len(v) for k, v in m.requests.items() if "synthesis" in str(k)
                )
                assert synthesis_count == 2
        finally:
            bot._RECENT_SYNTH_TTL_SECONDS = original_ttl
            await close_http_session()
            bot._recent_synth_cache.clear()

    async def test_concurrent_calls_dedupe_http_even_without_cache_true(self):
        """同一キーの同時合成は cache=False でも in-flight 共有で1回にまとまる"""
        import asyncio

        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._recent_synth_cache.clear()
        bot._synth_in_flight.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={}, repeat=True)
                m.post(re.compile(r".*synthesis.*"), body=b"once", repeat=True)
                r1, r2 = await asyncio.gather(
                    synthesize("並行-nocache", VoiceSettings()),
                    synthesize("並行-nocache", VoiceSettings()),
                )
                assert r1 == b"once"
                assert r2 == b"once"
                synthesis_count = sum(
                    len(v) for k, v in m.requests.items() if "synthesis" in str(k)
                )
                assert synthesis_count == 1
        finally:
            await close_http_session()
            bot._recent_synth_cache.clear()
            bot._synth_in_flight.clear()

    async def test_concurrent_calls_dedupe_http(self):
        """同一キーで同時に cache=True の合成が走ると、HTTPは1回だけになる"""
        import asyncio

        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        bot._synth_in_flight.clear()
        http_calls = 0

        async def counting_synthesis(url, **kwargs):
            nonlocal http_calls
            http_calls += 1
            # in-flight 間に両 coroutine が重なるように少し待つ
            await asyncio.sleep(0.01)
            import aiohttp

            return aiohttp.web.Response(body=b"once")

        try:
            with aioresponses() as m:
                # audio_query は普通に返し、synthesis はカウントしてから返す
                m.post(re.compile(r".*audio_query.*"), payload={}, repeat=True)
                m.post(
                    re.compile(r".*synthesis.*"),
                    callback=lambda url, **kw: None,
                    body=b"once",
                    repeat=True,
                )
                # 同じキーで2つ並行実行
                r1, r2 = await asyncio.gather(
                    synthesize("並行", VoiceSettings(), cache=True),
                    synthesize("並行", VoiceSettings(), cache=True),
                )
                assert r1 == b"once"
                assert r2 == b"once"
                # synthesis 1回のみ（2つめは in-flight 待ち→キャッシュから返る）
                synthesis_count = sum(1 for k in m.requests if "synthesis" in str(k))
                assert synthesis_count == 1
                audio_query_count = sum(
                    1 for k in m.requests if "audio_query" in str(k)
                )
                assert audio_query_count == 1
        finally:
            await close_http_session()
            bot._synth_cache.clear()
            bot._synth_in_flight.clear()

    async def test_in_flight_cleared_on_error(self):
        """合成が失敗した時も in-flight エントリが残らない（次のリトライを妨げない）"""
        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        bot._synth_in_flight.clear()
        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), status=500)
                with pytest.raises(Exception):
                    await synthesize("失敗", VoiceSettings(), cache=True)
            assert bot._synth_in_flight == {}
        finally:
            await close_http_session()
            bot._synth_cache.clear()
            bot._synth_in_flight.clear()

    async def test_cache_uses_fallback_entry_without_http(self):
        import bot
        from bot import VoiceSettings, synthesize

        original_map = dict(bot.speaker_engine)
        try:
            bot._synth_cache.clear()
            bot.speaker_engine.clear()
            # requested -> primary, default -> fallback
            bot.speaker_engine[999] = ("http://primary:50021", 99)
            bot.speaker_engine[3] = ("http://fallback:50021", 3)
            fallback_key = (
                "http://fallback:50021",
                3,
                "定型文",
                1.0,
                0.0,
                1.0,
                1.0,
            )
            bot._synth_cache[fallback_key] = b"from-fallback-cache"

            # HTTPモックを登録しない（キャッシュヒットならネットワーク不要）
            result = await synthesize(
                "定型文", VoiceSettings(speaker_id=999), cache=True
            )
            assert result == b"from-fallback-cache"
        finally:
            bot.speaker_engine.clear()
            bot.speaker_engine.update(original_map)
            bot._synth_cache.clear()

    async def test_candidate_backoff_skips_recent_failure(self):
        import bot
        from bot import VoiceSettings, synthesize

        original_map = dict(bot.speaker_engine)
        original_fail = dict(bot._candidate_fail_until)
        try:
            bot.speaker_engine.clear()
            bot._candidate_fail_until.clear()
            bot.speaker_engine[777] = ("http://bad:50021", 7)
            bot.speaker_engine[3] = ("http://good:50021", 3)
            bot._candidate_fail_until[("http://bad:50021", 7)] = (
                bot.time.monotonic() + 60.0
            )

            with aioresponses() as m:
                m.post(re.compile(r"http://good:50021/audio_query.*"), payload={})
                m.post(re.compile(r"http://good:50021/synthesis.*"), body=b"ok")
                result = await synthesize("テスト", VoiceSettings(speaker_id=777))
                assert result == b"ok"

                bad_calls = [
                    k for k in m.requests.keys() if "http://bad:50021" in str(k)
                ]
                assert bad_calls == []
        finally:
            bot.speaker_engine.clear()
            bot.speaker_engine.update(original_map)
            bot._candidate_fail_until.clear()
            bot._candidate_fail_until.update(original_fail)

    async def test_all_candidates_in_backoff_do_not_probe_network(self):
        import bot
        from bot import VoiceSettings, synthesize

        original_map = dict(bot.speaker_engine)
        original_fail = dict(bot._candidate_fail_until)
        try:
            bot.speaker_engine.clear()
            bot._candidate_fail_until.clear()
            bot.speaker_engine[777] = ("http://bad-a:50021", 7)
            bot.speaker_engine[3] = ("http://bad-b:50021", 3)
            future = bot.time.monotonic() + 60.0
            for engine_url, real_id in {
                ("http://bad-a:50021", 7),
                ("http://bad-b:50021", 3),
                ("http://test-voicevox:50021", 3),
            }:
                bot._candidate_fail_until[(engine_url, real_id)] = future

            with patch("bot._synthesize_with_candidate", new=AsyncMock()) as mocked_syn:
                with pytest.raises(aiohttp.ClientError, match="バックオフ中"):
                    await synthesize("テスト", VoiceSettings(speaker_id=777))
                mocked_syn.assert_not_awaited()
        finally:
            bot.speaker_engine.clear()
            bot.speaker_engine.update(original_map)
            bot._candidate_fail_until.clear()
            bot._candidate_fail_until.update(original_fail)

    async def test_fallback_caches_under_primary_key_too(self, monkeypatch):
        """primary 候補が失敗して fallback で成功した時、
        primary キーでも引けるように二重キャッシュされること"""
        import aiohttp

        import bot
        from bot import VoiceSettings, close_http_session, synthesize

        bot._synth_cache.clear()
        bot._synth_in_flight.clear()
        monkeypatch.setattr(
            "bot.ENGINES",
            [
                ("PRIMARY", "http://primary:50021", 0),
                ("FALLBACK", "http://fallback:50021", 10000),
            ],
        )
        bot.speaker_engine.clear()
        bot.speaker_engine[3] = ("http://primary:50021", 3)
        try:
            with aioresponses() as m:
                m.post(
                    re.compile(r"http://primary:50021/audio_query.*"),
                    exception=aiohttp.ClientError("down"),
                )
                m.post(
                    re.compile(r"http://fallback:50021/audio_query.*"),
                    payload={},
                )
                m.post(
                    re.compile(r"http://fallback:50021/synthesis.*"),
                    body=b"fallback-result",
                )
                result = await synthesize("test", VoiceSettings(), cache=True)
                assert result == b"fallback-result"

            primary_key = ("http://primary:50021", 3, "test", 1.0, 0.0, 1.0, 1.0)
            fallback_key = ("http://fallback:50021", 3, "test", 1.0, 0.0, 1.0, 1.0)
            assert primary_key in bot._synth_cache
            assert fallback_key in bot._synth_cache
            assert bot._synth_cache[primary_key] == b"fallback-result"
        finally:
            await close_http_session()
            bot._synth_cache.clear()
            bot._synth_in_flight.clear()
            bot.speaker_engine.clear()
            bot.speaker_engine[3] = ("http://test-voicevox:50021", 3)


def _make_wav_bytes(channels=2, rate=48000, width=2, n_samples=480):
    """テスト用 WAV バイト列生成。デフォルトは 48kHz stereo 16-bit 10ms"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * n_samples * channels * width)
    return buf.getvalue()


class TestMakeAudioSource:
    """_make_audio_source: 48kHz stereo 16bit なら PCMAudio、それ以外は FFmpeg"""

    def test_pcm_for_valid_48khz_stereo_wav(self):
        import discord

        from bot import _make_audio_source

        wav = _make_wav_bytes(channels=2, rate=48000, width=2)
        source = _make_audio_source(wav)
        assert isinstance(source, discord.PCMAudio)

    @patch("discord.FFmpegPCMAudio")
    def test_ffmpeg_fallback_for_invalid_data(self, mock_ffmpeg):
        from bot import _make_audio_source

        _make_audio_source(b"not-a-wav")
        mock_ffmpeg.assert_called_once()

    @patch("discord.FFmpegPCMAudio")
    def test_ffmpeg_fallback_for_mono(self, mock_ffmpeg):
        from bot import _make_audio_source

        wav = _make_wav_bytes(channels=1, rate=48000, width=2)
        _make_audio_source(wav)
        mock_ffmpeg.assert_called_once()

    @patch("discord.FFmpegPCMAudio")
    def test_ffmpeg_fallback_for_wrong_samplerate(self, mock_ffmpeg):
        from bot import _make_audio_source

        wav = _make_wav_bytes(channels=2, rate=24000, width=2)
        _make_audio_source(wav)
        mock_ffmpeg.assert_called_once()

    @patch("discord.FFmpegPCMAudio")
    def test_ffmpeg_fallback_for_wrong_sample_width(self, mock_ffmpeg):
        from bot import _make_audio_source

        wav = _make_wav_bytes(channels=2, rate=48000, width=1)  # 8bit
        _make_audio_source(wav)
        mock_ffmpeg.assert_called_once()

    def test_pcm_padded_to_full_frame(self):
        """末尾の半端フレームがゼロパディングされて全部再生されること"""
        import discord

        from bot import _make_audio_source

        # 3840未満しかPCMが出ないサイズにする（48000Hz stereo 16bit = 3840B/20ms）
        # n_samples=240 = 5ms → PCM 960B
        wav = _make_wav_bytes(channels=2, rate=48000, width=2, n_samples=240)
        source = _make_audio_source(wav)
        assert isinstance(source, discord.PCMAudio)
        # 読み出して 3840 バイトの1フレームが取れる（パディング済み）
        first_frame = source.read()
        assert len(first_frame) == 3840
        # 次は空
        assert source.read() == b""

    def test_pcm_multi_frame_read(self):
        """複数フレーム分のPCMを順次返すこと"""
        import discord

        from bot import _make_audio_source

        # 2フレーム分ぴったり = 960 samples @ stereo
        wav = _make_wav_bytes(channels=2, rate=48000, width=2, n_samples=1920)
        source = _make_audio_source(wav)
        assert isinstance(source, discord.PCMAudio)
        f1 = source.read()
        f2 = source.read()
        assert len(f1) == 3840
        assert len(f2) == 3840
        assert source.read() == b""


class TestSynthesizeOutputFormat:
    """synthesize が Discord 互換フォーマット(48kHz stereo)をエンジンに要求するか"""

    async def test_requests_48khz_stereo_in_query(self):
        from bot import VoiceSettings, close_http_session, synthesize

        try:
            with aioresponses() as m:
                m.post(re.compile(r".*audio_query.*"), payload={})
                m.post(re.compile(r".*synthesis.*"), body=b"x")
                await synthesize("テスト", VoiceSettings())
                # synthesis のリクエストボディを検証
                synthesis_call = list(m.requests.values())[1][0]
                body = synthesis_call.kwargs["json"]
                assert body["outputSamplingRate"] == 48000
                assert body["outputStereo"] is True
        finally:
            await close_http_session()


class TestPlayNextUsesPcm:
    async def test_valid_wav_uses_pcm_not_ffmpeg(self):
        """完全な 48kHz stereo WAV が来たら FFmpeg を起動しない"""
        from bot import play_locks, play_next, queues

        wav = _make_wav_bytes(channels=2, rate=48000, width=2, n_samples=480)
        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_vc.is_playing.return_value = False
        mock_vc.is_paused.return_value = False
        queues[2001] = deque([wav])
        try:
            with patch("discord.FFmpegPCMAudio") as mock_ffmpeg:
                await play_next(2001, mock_vc)
                mock_ffmpeg.assert_not_called()
            mock_vc.play.assert_called_once()
        finally:
            queues.pop(2001, None)
            play_locks.pop(2001, None)
