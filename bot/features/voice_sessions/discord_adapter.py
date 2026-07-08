"""voice-session 復旧用の Discord adapter。"""

from __future__ import annotations

from typing import Any

import discord


async def handle_voice_state_update(
    ctx: Any,
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """session と再生継続のために Discord voice-state event を処理する。"""
    if member.bot:
        is_self_event = ctx.client.user is not None and member.id == ctx.client.user.id
        if is_self_event:
            guild_id = member.guild.id
            if after.channel is None:
                ctx._cleanup_guild_playback_state(guild_id)
                ctx._spawn_background(ctx._safe_forget_voice_session(guild_id))
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
