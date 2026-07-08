"""voice-session 復旧用の Discord adapter。"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

import discord

SELF_VOICE_RECOVERY_INITIAL_DELAY_SECONDS = 2.0
SELF_VOICE_RECOVERY_TIMEOUT_SECONDS = 30.0
SELF_VOICE_RECOVERY_POLL_SECONDS = 1.0
READY_VOICE_RESTORE_DELAY_SECONDS = 35.0
GATEWAY_SERVER_ERROR_MIN_STATUS = 500
GATEWAY_SERVER_ERROR_MAX_STATUS = 599
GATEWAY_RECOVERABLE_DISCONNECT_RESTORE_WINDOW_SECONDS = 180.0
GATEWAY_SESSION_INVALIDATED_MESSAGE = "session has been invalidated"
USER_REQUESTED_DISCONNECT_WINDOW_SECONDS = 60.0
MANUAL_VOICE_DISCONNECT_AUDIT_WINDOW_SECONDS = 15.0


class VoiceDisconnectMeaning(Enum):
    """Bot 自身の VC 切断を、復旧すべきかどうかで分類する。"""

    USER_REQUESTED = "user_requested"
    RECOVERABLE = "recoverable"


def _is_recent_monotonic_timestamp(
    timestamp: float | None,
    window_seconds: float,
) -> bool:
    """monotonic 秒の記録が指定 window 内なら True を返す。"""
    if timestamp is None:
        return False
    return time.monotonic() - timestamp <= window_seconds


def _is_gateway_server_error_status(status: object) -> bool:
    """gateway 再接続の手掛かりにする 5xx status かを判定する。"""
    return (
        isinstance(status, int)
        and GATEWAY_SERVER_ERROR_MIN_STATUS <= status <= GATEWAY_SERVER_ERROR_MAX_STATUS
    )


class DiscordGatewayRecoverableDisconnectLogHandler(logging.Handler):
    """discord.py の gateway 復旧対象ログを拾って runtime に記録する。"""

    def __init__(self, callback: Any) -> None:
        """復旧対象ログを検出した時に呼ぶ callback を保持する。"""
        super().__init__(level=logging.INFO)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        """5xx 系と session invalidated を復旧対象 gateway 断として扱う。"""
        if self._is_gateway_session_invalidated(record):
            self._callback()
            return

        if record.exc_info is None:
            return

        error = record.exc_info[1]
        status = getattr(error, "status", None)
        if _is_gateway_server_error_status(status):
            self._callback()

    def _is_gateway_session_invalidated(self, record: logging.LogRecord) -> bool:
        """discord.gateway の session invalidated ログかを判定する。"""
        return (
            record.name == "discord.gateway"
            and GATEWAY_SESSION_INVALIDATED_MESSAGE in record.getMessage()
        )


def install_gateway_recoverable_disconnect_log_handler(ctx: Any) -> None:
    """gateway 復旧対象ログの監視 handler を一度だけ取り付ける。"""
    handler = getattr(ctx, "_gateway_recoverable_disconnect_log_handler", None)
    if handler is not None:
        return

    handler = DiscordGatewayRecoverableDisconnectLogHandler(
        lambda: record_gateway_recoverable_disconnect(ctx),
    )
    loggers = (
        logging.getLogger("discord.client"),
        logging.getLogger("discord.gateway"),
    )
    for gateway_logger in loggers:
        gateway_logger.addHandler(handler)

    ctx._gateway_recoverable_disconnect_log_handler = handler
    ctx._gateway_recoverable_disconnect_loggers = loggers


def uninstall_gateway_recoverable_disconnect_log_handler(ctx: Any) -> None:
    """gateway 復旧対象ログの監視 handler を取り外す。"""
    handler = getattr(ctx, "_gateway_recoverable_disconnect_log_handler", None)
    if handler is None:
        return

    for gateway_logger in getattr(ctx, "_gateway_recoverable_disconnect_loggers", ()):
        try:
            gateway_logger.removeHandler(handler)
        except ValueError:
            pass

    ctx._gateway_recoverable_disconnect_log_handler = None
    ctx._gateway_recoverable_disconnect_loggers = ()


def begin_shutdown(ctx: Any) -> None:
    """終了処理中の VC 切断を復旧対象にも手動切断にも分類しないよう印を付ける。"""
    ctx._shutting_down = True
    for recovery_task in list(get_self_voice_recovery_tasks(ctx).values()):
        recovery_task.cancel()
    ready_restore_task = getattr(ctx, "_ready_voice_restore_task", None)
    if ready_restore_task is not None:
        ready_restore_task.cancel()


def get_self_voice_recovery_tasks(ctx: Any) -> dict[int, Any]:
    """guild ごとの自己 VC 切断復旧 task 管理表を返す。"""
    recovery_tasks = cast(
        dict[int, Any] | None,
        getattr(ctx, "_self_voice_recovery_tasks", None),
    )
    if recovery_tasks is None:
        recovery_tasks = {}
        ctx._self_voice_recovery_tasks = recovery_tasks
    return recovery_tasks


def record_gateway_recoverable_disconnect(ctx: Any) -> None:
    """gateway の復旧対象切断を monotonic 秒で記録する。"""
    ctx._last_gateway_recoverable_disconnect_at = time.monotonic()


def record_user_requested_disconnect(ctx: Any, guild_id: int) -> None:
    """パネル/コマンド由来の明示的な VC 切断を guild 単位で記録する。"""
    disconnects_by_guild = getattr(
        ctx,
        "_last_user_requested_disconnect_at_by_guild",
        None,
    )
    if disconnects_by_guild is None:
        disconnects_by_guild = {}
        ctx._last_user_requested_disconnect_at_by_guild = disconnects_by_guild
    disconnects_by_guild[guild_id] = time.monotonic()


def has_recent_user_requested_disconnect(ctx: Any, guild_id: int) -> bool:
    """直近にユーザー操作で同 guild の VC 切断を要求していたかを返す。"""
    disconnects_by_guild = getattr(
        ctx,
        "_last_user_requested_disconnect_at_by_guild",
        {},
    )
    return _is_recent_monotonic_timestamp(
        disconnects_by_guild.get(guild_id),
        USER_REQUESTED_DISCONNECT_WINDOW_SECONDS,
    )


def has_recent_gateway_recoverable_disconnect(ctx: Any) -> bool:
    """直近に gateway 起因の復旧対象切断があったかを返す。"""
    return _is_recent_monotonic_timestamp(
        getattr(ctx, "_last_gateway_recoverable_disconnect_at", None),
        GATEWAY_RECOVERABLE_DISCONNECT_RESTORE_WINDOW_SECONDS,
    )


def schedule_delayed_voice_session_restore(ctx: Any) -> None:
    """ready 再発火後、Discord 状態が落ち着いてから saved session 復旧を走らせる。"""
    ready_restore_task = getattr(ctx, "_ready_voice_restore_task", None)
    if ready_restore_task is not None and not ready_restore_task.done():
        return
    ctx._ready_voice_restore_task = ctx.asyncio.create_task(
        restore_voice_sessions_after_ready_delay(ctx),
    )


async def restore_voice_sessions_after_ready_delay(ctx: Any) -> None:
    """gateway 再接続後の遅延 VC session 復旧 task 本体。"""
    try:
        await ctx.asyncio.sleep(READY_VOICE_RESTORE_DELAY_SECONDS)
        if getattr(ctx, "_shutting_down", False):
            return
        await ctx._restore_voice_sessions_on_startup()
    except ctx.asyncio.CancelledError:
        pass
    except Exception as e:
        ctx.logger.exception(f"gateway 再接続後の VC 復旧に失敗: {e}")


async def handle_voice_state_update(
    ctx: Any,
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """session と再生継続のために Discord voice-state event を処理する。"""
    if before.channel == after.channel:
        return

    if member.bot:
        is_self_event = ctx.client.user is not None and member.id == ctx.client.user.id
        if is_self_event:
            guild_id = member.guild.id
            if after.channel is None:
                await handle_self_voice_disconnect(ctx, member.guild, before, after)
                return

            vc = ctx._as_voice_client(member.guild.voice_client)
            queue = ctx.queues.get(guild_id)
            if vc and queue and ctx._can_start_playback(vc):
                await ctx.play_next(guild_id, vc)

            before_channel_id = getattr(before.channel, "id", None)
            after_channel_id = getattr(after.channel, "id", None)
            self_moved_channels = (
                before.channel is not None
                and after_channel_id is not None
                and before_channel_id != after_channel_id
            )
            if self_moved_channels and vc and ctx._is_vc_connected(vc):
                bot_channel = vc.channel
                non_bot_members = [m for m in bot_channel.members if not m.bot]
                if not non_bot_members:
                    try:
                        await ctx.forget_voice_session(guild_id)
                    except Exception as e:
                        ctx.logger.warning(f"VCセッション削除に失敗: {e}")
                    await ctx._safe_disconnect(vc)
                    ctx._cleanup_guild_state(guild_id)
                    ctx.logger.info(
                        f"BotのみのVCへ移動されたため自動切断 (Guild: {guild_id})"
                    )
                    return

            text_channel_id = ctx.read_channels.get(guild_id)
            if vc and ctx._is_vc_connected(vc) and text_channel_id is not None:
                ctx._spawn_background(
                    ctx._safe_record_voice_session(
                        guild_id, after.channel.id, text_channel_id
                    )
                )
            return

    vc = ctx._as_voice_client(member.guild.voice_client)
    if vc is None or not vc.is_connected():
        return

    guild_id = member.guild.id
    bot_channel = vc.channel

    members = [m for m in bot_channel.members if not m.bot]
    if not members:
        try:
            await ctx.forget_voice_session(guild_id)
        except Exception as e:
            ctx.logger.warning(f"VCセッション削除に失敗: {e}")
        await ctx._safe_disconnect(vc)
        ctx._cleanup_guild_state(guild_id)
        ctx.logger.info(f"全員退出のため自動切断 (Guild: {guild_id})")
        return

    if member.bot:
        return

    joined = before.channel != bot_channel and after.channel == bot_channel
    left = before.channel == bot_channel and after.channel != bot_channel

    if joined or left:
        name = member.display_name
        text = (
            f"{name}さんがにゅうしつしました"
            if joined
            else f"{name}さんがたいしつしました"
        )
        try:
            vc = ctx._as_voice_client(member.guild.voice_client)
            if vc is None or not vc.is_connected():
                return

            settings = ctx.get_user_settings(member.guild.id, member.id)
            audio_data = await ctx.synthesize(text, settings, cache=True)

            vc = ctx._as_voice_client(member.guild.voice_client)
            if vc is None or not vc.is_connected():
                return

            ctx._ensure_queue(guild_id).append(audio_data)

            if ctx._can_start_playback(vc):
                await ctx.play_next(guild_id, vc)
        except discord.ClientException:
            ctx.logger.info("入退室通知をスキップ: BotがVC未接続")
        except Exception as e:
            ctx.logger.error(f"入退室通知の音声合成エラー: {e}")


async def handle_self_voice_disconnect(
    ctx: Any,
    guild: discord.Guild,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Bot 自身が VC から外れた時、即削除せず復旧対象かを判定する。"""
    if before.channel is None or after.channel is not None:
        return
    if getattr(ctx, "_shutting_down", False):
        ctx.logger.info(f"終了処理中の自己 VC 切断を無視 guild={guild.id}")
        return

    ctx._cleanup_guild_playback_state(guild.id)

    recovery_tasks = get_self_voice_recovery_tasks(ctx)
    recovery_task = recovery_tasks.get(guild.id)
    if recovery_task is not None and not recovery_task.done():
        return

    recovery_tasks[guild.id] = ctx.asyncio.create_task(
        resolve_self_voice_disconnect(ctx, guild, before.channel),
    )


async def resolve_self_voice_disconnect(
    ctx: Any,
    guild: discord.Guild,
    previous_channel: object,
) -> None:
    """自己 VC 切断後、auto-reconnect 待機か saved session 復旧かを選ぶ。"""
    try:
        if getattr(ctx, "_shutting_down", False):
            return

        voice_client = await wait_for_recovered_voice_client(ctx, guild)
        if getattr(ctx, "_shutting_down", False):
            return
        if voice_client is not None:
            await rehydrate_recovered_voice_client(
                ctx,
                guild,
                voice_client,
                previous_channel,
            )
            return

        restored = await restore_after_voice_disconnect(ctx, guild, previous_channel)
        if restored:
            return

        await handle_confirmed_self_voice_disconnect(ctx, guild.id)
    except Exception as e:
        ctx.logger.exception(f"自己 VC 切断の解決に失敗 guild={guild.id}: {e}")
    finally:
        recovery_tasks = get_self_voice_recovery_tasks(ctx)
        if recovery_tasks.get(guild.id) is ctx.asyncio.current_task():
            recovery_tasks.pop(guild.id, None)


async def wait_for_recovered_voice_client(
    ctx: Any,
    guild: discord.Guild,
) -> discord.VoiceClient | None:
    """discord.py の auto-reconnect で voice client が戻るか短時間待つ。"""
    timeout = max(0.0, SELF_VOICE_RECOVERY_TIMEOUT_SECONDS)
    initial_delay = min(SELF_VOICE_RECOVERY_INITIAL_DELAY_SECONDS, timeout)
    deadline = ctx.asyncio.get_running_loop().time() + timeout

    if initial_delay > 0:
        await ctx.asyncio.sleep(initial_delay)

    while True:
        voice_client = ctx._as_voice_client(guild.voice_client)
        if voice_client is None:
            return None
        if ctx._is_vc_connected(voice_client):
            return voice_client

        remaining = deadline - ctx.asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        await ctx.asyncio.sleep(min(SELF_VOICE_RECOVERY_POLL_SECONDS, remaining))


async def rehydrate_recovered_voice_client(
    ctx: Any,
    guild: discord.Guild,
    voice_client: discord.VoiceClient,
    previous_channel: object,
) -> None:
    """auto-reconnect 済み voice client を読み上げ用メモリ状態へ戻す。"""
    guild_id = guild.id
    text_channel_id = await resolve_text_channel_id_for_restore(ctx, guild_id)
    if text_channel_id is None:
        ctx.logger.info(
            f"VC は復帰済みだが読み上げ先 text channel が不明 guild={guild_id}"
        )
        return

    channel = getattr(voice_client, "channel", None) or previous_channel
    voice_channel_id = getattr(channel, "id", None)
    if not isinstance(voice_channel_id, int):
        ctx.logger.info(f"VC は復帰済みだが voice channel が不明 guild={guild_id}")
        return

    ctx.queues[guild_id] = ctx._new_queue()
    ctx.read_channels[guild_id] = text_channel_id
    ctx._spawn_background(
        ctx._safe_record_voice_session(guild_id, voice_channel_id, text_channel_id)
    )
    ctx.logger.info(
        f"一時切断後の VC auto-reconnect を反映 guild={guild_id} "
        f"channel={voice_channel_id}"
    )


async def restore_after_voice_disconnect(
    ctx: Any,
    guild: discord.Guild,
    previous_channel: object,
) -> bool:
    """復旧対象の自己切断なら保存済み VC session へ戻す。"""
    disconnect_meaning = await classify_self_voice_disconnect(ctx, guild)
    if disconnect_meaning is VoiceDisconnectMeaning.USER_REQUESTED:
        ctx.logger.info(f"自己 VC 切断をユーザー操作として扱います guild={guild.id}")
        return False

    text_channel_id = await resolve_text_channel_id_for_restore(ctx, guild.id)
    voice_channel_id = getattr(previous_channel, "id", None)
    if text_channel_id is None or not isinstance(voice_channel_id, int):
        session = await load_saved_session_for_guild(ctx, guild.id)
        if session is not None:
            _, voice_channel_id, text_channel_id = session

    if not isinstance(voice_channel_id, int) or not isinstance(text_channel_id, int):
        ctx.logger.warning(f"VC 復旧に必要な session 情報がありません guild={guild.id}")
        return False

    await ctx._reconnect_vc(guild.id, voice_channel_id, text_channel_id)
    voice_client = ctx._as_voice_client(guild.voice_client)
    if voice_client is not None and ctx._is_vc_connected(voice_client):
        ctx.logger.info(
            f"自己 VC 切断後に保存済み session へ復旧 guild={guild.id} "
            f"channel={voice_channel_id}"
        )
        return True
    return True


async def resolve_text_channel_id_for_restore(ctx: Any, guild_id: int) -> int | None:
    """復旧時に使う text channel ID をメモリ優先、DB fallback で解決する。"""
    text_channel_id = ctx.read_channels.get(guild_id)
    if isinstance(text_channel_id, int):
        return text_channel_id

    session = await load_saved_session_for_guild(ctx, guild_id)
    if session is None:
        return None
    _, _, saved_text_channel_id = session
    return saved_text_channel_id


async def load_saved_session_for_guild(
    ctx: Any,
    guild_id: int,
) -> tuple[int, int, int] | None:
    """active_voice_sessions から guild 1 件分の保存 session を探す。"""
    try:
        sessions = await ctx.load_voice_sessions()
    except Exception as e:
        ctx.logger.warning(f"VC session 読み込み失敗 guild={guild_id}: {e}")
        return None
    for session in sessions:
        if session[0] == guild_id:
            return session
    return None


async def classify_self_voice_disconnect(
    ctx: Any,
    guild: discord.Guild,
) -> VoiceDisconnectMeaning:
    """自己 VC 切断をユーザー操作か復旧対象かに分類する。"""
    guild_id = guild.id
    if has_recent_user_requested_disconnect(ctx, guild_id):
        return VoiceDisconnectMeaning.USER_REQUESTED
    if has_recent_gateway_recoverable_disconnect(ctx):
        return VoiceDisconnectMeaning.RECOVERABLE
    if await has_recent_manual_voice_disconnect_audit_entry(ctx, guild):
        return VoiceDisconnectMeaning.USER_REQUESTED
    return VoiceDisconnectMeaning.RECOVERABLE


async def has_recent_manual_voice_disconnect_audit_entry(
    ctx: Any,
    guild: discord.Guild,
) -> bool:
    """直近の audit log に手動 VC 切断があるか確認する。"""
    audit_logs = getattr(guild, "audit_logs", None)
    if audit_logs is None:
        return False

    now = datetime.now(UTC)
    try:
        async for entry in audit_logs(
            limit=5,
            action=discord.AuditLogAction.member_disconnect,
        ):
            disconnected_count = getattr(getattr(entry, "extra", None), "count", None)
            if disconnected_count != 1:
                continue
            created_at = getattr(entry, "created_at", None)
            if created_at is None:
                continue
            age_seconds = abs((now - created_at).total_seconds())
            if age_seconds <= MANUAL_VOICE_DISCONNECT_AUDIT_WINDOW_SECONDS:
                return True
            if age_seconds > MANUAL_VOICE_DISCONNECT_AUDIT_WINDOW_SECONDS:
                return False
    except discord.Forbidden:
        ctx.logger.warning(f"audit log を参照できません guild={guild.id}")
    except discord.HTTPException as e:
        ctx.logger.warning(f"audit log 参照に失敗 guild={guild.id}: {e}")
    except Exception as e:
        ctx.logger.exception(f"audit log 判定で予期せぬ失敗 guild={guild.id}: {e}")

    return False


async def handle_confirmed_self_voice_disconnect(ctx: Any, guild_id: int) -> None:
    """復旧対象ではない自己 VC 切断として session とメモリ状態を削除する。"""
    if getattr(ctx, "_shutting_down", False):
        return
    await ctx._safe_forget_voice_session(guild_id)
    ctx._cleanup_guild_state(guild_id)
    ctx.logger.info(f"Bot が VC から手動切断されたため session を削除 guild={guild_id}")


async def reconnect_vc(
    ctx: Any, guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """以前に記録した Discord voice session へ再接続する。"""
    if guild_id in ctx._vc_reconnect_inflight:
        ctx.logger.info(f"VC復旧は既に進行中、重複起動をスキップ guild={guild_id}")
        return
    ctx._vc_reconnect_inflight.add(guild_id)
    try:
        guild = ctx.client.get_guild(guild_id)
        if guild is None:
            ctx.logger.warning(f"VC復旧失敗（ギルド未参加） guild={guild_id}")
            await ctx.forget_voice_session(guild_id)
            return

        channel = guild.get_channel(voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            ctx.logger.warning(
                f"VC復旧失敗（VCが見つからない） guild={guild_id} "
                f"channel={voice_channel_id}"
            )
            await ctx.forget_voice_session(guild_id)
            return

        non_bot_members = [member for member in channel.members if not member.bot]
        if not non_bot_members:
            ctx.logger.info(
                f"VC復旧抑止 guild={guild_id} channel={voice_channel_id}: "
                "部屋に人がいないため復帰しません"
            )
            await ctx.forget_voice_session(guild_id)
            return

        me = guild.me
        if me is not None:
            perms = channel.permissions_for(me)
            if not perms.connect or not perms.speak:
                ctx.logger.warning(
                    f"VC復旧抑止 guild={guild_id} channel={voice_channel_id}: "
                    "接続/発言権限がありません"
                )
                await ctx.forget_voice_session(guild_id)
                return

        def ensure_session_memory() -> None:
            """有効な VC session をインメモリ再生状態へ反映する。"""
            ctx.queues[guild_id] = ctx._new_queue()
            ctx.read_channels[guild_id] = text_channel_id

        for attempt in range(ctx.VC_RECONNECT_MAX_ATTEMPTS):
            existing = ctx._as_voice_client(guild.voice_client)
            if existing and ctx._is_vc_connected(existing):
                ensure_session_memory()
                try:
                    await guild.change_voice_state(channel=channel, self_deaf=True)
                except Exception as e:
                    ctx.logger.warning(f"self_deaf 設定に失敗 guild={guild_id}: {e}")
                ctx.logger.info(f"VC既に接続中、メモリ状態を再反映 guild={guild_id}")
                return
            try:
                await channel.connect(self_deaf=True)
                ensure_session_memory()
                ctx.logger.info(
                    f"VC復旧成功 guild={guild_id} channel={voice_channel_id}"
                )
                return
            except Exception as e:
                wait = min(
                    ctx.VC_RECONNECT_BACKOFF_BASE_SECONDS * (2**attempt),
                    ctx.VC_RECONNECT_BACKOFF_MAX_SECONDS,
                )
                ctx.logger.warning(
                    f"VC復旧失敗 ({attempt + 1}/{ctx.VC_RECONNECT_MAX_ATTEMPTS}) "
                    f"guild={guild_id}: {e} → {wait}秒後に再試行"
                )
                await ctx.asyncio.sleep(wait)

        ctx.logger.error(
            f"VC復旧諦め guild={guild_id}: {ctx.VC_RECONNECT_MAX_ATTEMPTS}回失敗"
        )
        await ctx.forget_voice_session(guild_id)
    except Exception as e:
        ctx.logger.error(f"VC復旧で予期せぬエラー guild={guild_id}: {e}")
    finally:
        ctx._vc_reconnect_inflight.discard(guild_id)


async def safe_forget_voice_session(ctx: Any, guild_id: int) -> None:
    """DB error を warning ログに留めつつ voice session を忘れる。"""
    try:
        await ctx.forget_voice_session(guild_id)
    except Exception as e:
        ctx.logger.warning(f"VCセッション削除失敗 guild={guild_id}: {e}")


async def safe_record_voice_session(
    ctx: Any, guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """DB error を warning ログに留めつつ voice session を記録する。"""
    try:
        await ctx.record_voice_session(guild_id, voice_channel_id, text_channel_id)
    except Exception as e:
        ctx.logger.warning(f"VCセッション再保存失敗 guild={guild_id}: {e}")


async def restore_voice_sessions_on_startup(ctx: Any) -> None:
    """記録済み Discord voice session を起動時に順番に復旧する。"""
    try:
        sessions = await ctx.load_voice_sessions()
    except Exception as e:
        ctx.logger.warning(f"起動時 VC セッション読み込み失敗: {e}")
        return
    if not sessions:
        return
    ctx.logger.info(f"VC復旧を開始: {len(sessions)}件")
    for guild_id, voice_channel_id, text_channel_id in sessions:
        await ctx._reconnect_vc(guild_id, voice_channel_id, text_channel_id)
