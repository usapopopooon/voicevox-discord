import re
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestPlayNextAdditional:
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
        queues.pop(1004, None)
        play_locks.pop(1004, None)

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
        from bot import VoiceSettings, synthesize

        # speaker_engine が未登録 → DEFAULT_SPEAKER=3 でエンジンに投げる
        with aioresponses() as m:
            m.post(re.compile(r".*audio_query.*"), payload={})
            m.post(re.compile(r".*synthesis.*"), body=b"fallback-data")
            result = await synthesize("テスト", VoiceSettings(speaker_id=99999))
            assert result == b"fallback-data"
            audio_query_call = list(m.requests.values())[0][0]
            assert audio_query_call.kwargs["params"]["speaker"] == 3

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

        from bot import on_message, read_channels

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
        # ClientError パスはユーザー通知あり
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


class TestJoinCommand:
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
