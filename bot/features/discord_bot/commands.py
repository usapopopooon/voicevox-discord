"""Discord bot feature の slash command handler。

この module は Discord command の振る舞いを所有する。top-level の ``bot.py`` は
decorator registration だけを保持し、runtime context をこれらの handler へ渡す。
これにより、test と既存 import は古い command 名を使い続けられる。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal, cast

import discord
from discord import app_commands

PanelResponseMode = Literal["post", "update"]


async def _send_message(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool,
) -> None:
    """必要な場合だけ ephemeral を付けて interaction response を返す。"""
    if ephemeral:
        await interaction.response.send_message(content, ephemeral=True)
        return
    await interaction.response.send_message(content)


async def _followup_message(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool,
) -> None:
    """defer 後の追加応答を公開/非公開で出し分ける。"""
    if ephemeral:
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.followup.send(content)


async def _refresh_panel_or_report(
    ctx: Any,
    interaction: discord.Interaction,
    guild: discord.Guild,
    completed_message: str,
) -> None:
    """panel 更新に失敗した場合だけ、完了と再投稿手段を private に伝える。"""
    refreshed = await ctx._refresh_panel_message(interaction, guild)
    if refreshed:
        return
    await _followup_message(
        interaction,
        (
            f"{completed_message}\n"
            "パネルを更新できなかったため、`/panel` で再投稿してください。"
        ),
        ephemeral=True,
    )


async def join(
    ctx: Any,
    interaction: discord.Interaction,
    *,
    panel_response: PanelResponseMode = "post",
) -> None:
    """実行者の VC に接続し、現在の text channel の読み上げを開始する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    trace_id = ctx._new_trace_id()
    ctx._log_event(
        logging.INFO,
        "voice.connect.start",
        trace_id=trace_id,
        guild_id=guild.id,
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
    )
    private_errors = panel_response == "update"

    invoker = cast(discord.Member, interaction.user)
    if invoker.voice is None or invoker.voice.channel is None:
        await _send_message(
            interaction,
            "先にボイスチャンネルに入ってください",
            ephemeral=private_errors,
        )
        return

    channel = invoker.voice.channel

    text_channel_id = interaction.channel_id
    if text_channel_id is None:
        await _send_message(
            interaction,
            "テキストチャンネル情報を取得できませんでした",
            ephemeral=private_errors,
        )
        return

    me = guild.me
    if me is None and ctx.client.user is not None:
        me = guild.get_member(ctx.client.user.id)
    if me is None:
        await _send_message(
            interaction,
            "Botの権限情報を取得できませんでした。しばらく待ってから再試行してください",
            ephemeral=private_errors,
        )
        return

    perms = channel.permissions_for(me)
    if not perms.connect:
        await _send_message(
            interaction,
            "そのVCに接続する権限がありません",
            ephemeral=private_errors,
        )
        return
    if not perms.speak:
        await _send_message(
            interaction,
            "そのVCで発言する権限がありません",
            ephemeral=private_errors,
        )
        return
    if channel.user_limit and len(channel.members) >= channel.user_limit:
        if not perms.manage_channels:
            await _send_message(
                interaction,
                "VCの人数制限に達しています",
                ephemeral=private_errors,
            )
            return

    if panel_response == "update":
        await interaction.response.defer()
    else:
        await interaction.response.defer(thinking=True)

    existing_active = ctx._has_active_voice_connection(guild)
    try:
        if existing_active:
            existing_vc = ctx._as_voice_client(guild.voice_client)
            if existing_vc is not None:
                await existing_vc.move_to(channel)
        else:
            await ctx._reset_voice_state(guild)
            await channel.connect(self_deaf=True)
    except Exception as e:
        ctx._record_recent_error(
            "voice.connect.failed", str(e), trace_id, guild_id=guild.id
        )
        ctx._log_event(
            logging.WARNING,
            "voice.connect.failed",
            trace_id=trace_id,
            guild_id=guild.id,
            user_id=interaction.user.id,
            voice_channel_id=getattr(channel, "id", None),
            error=str(e),
        )
        await _followup_message(
            interaction,
            "VCへの接続に失敗しました。権限やVCの状態を確認してから再試行してください。",
            ephemeral=private_errors,
        )
        return

    if existing_active:
        ctx._ensure_queue(guild.id)
    else:
        ctx.queues[guild.id] = ctx._new_queue()
    ctx.read_channels[guild.id] = text_channel_id

    try:
        await ctx.record_voice_session(guild.id, channel.id, text_channel_id)
    except Exception as e:
        ctx.logger.warning(f"VCセッション保存に失敗: {e}")

    notice = (
        f"「{channel.name}」に接続しました\nこのチャンネルのメッセージを読み上げます"
    )
    if panel_response == "update":
        await _refresh_panel_or_report(
            ctx,
            interaction,
            guild,
            f"「{channel.name}」に接続しました。",
        )
    else:
        await interaction.followup.send(
            embed=ctx._build_panel_embed(guild, notice=notice),
            view=ctx.ControlPanelView(guild),
        )
    ctx._log_event(
        logging.INFO,
        "voice.connect.succeeded",
        trace_id=trace_id,
        guild_id=guild.id,
        user_id=interaction.user.id,
        voice_channel_id=getattr(channel, "id", None),
        text_channel_id=text_channel_id,
        moved=existing_active,
    )

    try:
        settings = ctx.get_user_settings(guild.id, interaction.user.id)
        audio_data = await ctx.synthesize("せつぞくしました", settings, cache=True)
        vc = ctx._as_voice_client(guild.voice_client)
        if vc and ctx._is_vc_connected(vc):
            ctx.queues[guild.id].append(audio_data)
        vc = ctx._as_voice_client(guild.voice_client)
        if vc and ctx._is_vc_connected(vc) and ctx._can_start_playback(vc):
            await ctx.play_next(guild.id, vc)
    except Exception as e:
        ctx._record_recent_error(
            "voice.join_announcement.failed", str(e), trace_id, guild_id=guild.id
        )
        ctx.logger.error(f"接続挨拶の音声合成エラー: {e}")


async def leave(
    ctx: Any,
    interaction: discord.Interaction,
    *,
    panel_response: PanelResponseMode = "post",
) -> None:
    """VC から切断し、guild の再生/session 状態を消す。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    trace_id = ctx._new_trace_id()

    if ctx._has_active_voice_connection(guild):
        if panel_response == "update":
            await interaction.response.defer()
        ctx._record_user_requested_disconnect(guild.id)
        try:
            await ctx.forget_voice_session(guild.id)
        except Exception as e:
            ctx.logger.warning(f"VCセッション削除に失敗: {e}")
        await ctx._safe_disconnect(ctx._as_voice_client(guild.voice_client))
        ctx._cleanup_guild_state(guild.id)
        ctx._log_event(
            logging.INFO,
            "voice.disconnect.requested",
            trace_id=trace_id,
            guild_id=guild.id,
            user_id=interaction.user.id,
        )
        if panel_response == "update":
            await _refresh_panel_or_report(ctx, interaction, guild, "切断しました。")
        else:
            await interaction.response.send_message("切断しました")
        return

    await ctx._reset_voice_state(guild)
    ctx._log_event(
        logging.INFO,
        "voice.disconnect.noop",
        trace_id=trace_id,
        guild_id=guild.id,
        user_id=interaction.user.id,
    )
    await _send_message(
        interaction,
        "ボイスチャンネルに接続していません",
        ephemeral=panel_response == "update",
    )


async def vc_toggle(ctx: Any, interaction: discord.Interaction) -> None:
    """実行 guild の Bot voice connection を切り替える。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    if ctx._has_active_voice_connection(guild):
        ctx._record_user_requested_disconnect(guild.id)
        try:
            await ctx.forget_voice_session(guild.id)
        except Exception as e:
            ctx.logger.warning(f"VCセッション削除に失敗: {e}")
        await ctx._safe_disconnect(ctx._as_voice_client(guild.voice_client))
        ctx._cleanup_guild_state(guild.id)
        await interaction.response.send_message("切断しました")
        return

    await ctx._reset_voice_state(guild)
    await join(ctx, interaction)


async def skip(
    ctx: Any,
    interaction: discord.Interaction,
    *,
    panel_response: PanelResponseMode = "post",
) -> None:
    """再生中の audio item があれば停止する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    trace_id = ctx._new_trace_id()

    vc = ctx._as_voice_client(guild.voice_client)
    if vc is None or not ctx._is_vc_playing(vc):
        await _send_message(
            interaction,
            "再生中の音声はありません",
            ephemeral=panel_response == "update",
        )
        return
    if panel_response == "update":
        await interaction.response.defer()
    try:
        vc.stop()
    except discord.ClientException:
        if panel_response == "update":
            await _followup_message(
                interaction,
                "再生中の音声はありません",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("再生中の音声はありません")
        return
    ctx._log_event(
        logging.INFO,
        "queue.skip",
        trace_id=trace_id,
        guild_id=guild.id,
        user_id=interaction.user.id,
        queue_length=len(ctx.queues.get(guild.id, [])),
    )
    if panel_response == "update":
        await _refresh_panel_or_report(ctx, interaction, guild, "スキップしました。")
    else:
        await interaction.response.send_message("スキップしました")


async def mute(
    ctx: Any, interaction: discord.Interaction, user: discord.Member
) -> None:
    """現在の guild でユーザー 1 人の message 読み上げをミュートする。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    if user.bot:
        await interaction.response.send_message("Botはミュートできません")
        return
    await ctx.add_mute(guild.id, user.id)
    await interaction.response.send_message(f"{user.display_name} をミュートしました")


async def unmute(
    ctx: Any, interaction: discord.Interaction, user: discord.Member
) -> None:
    """guild におけるユーザー 1 人のミュートを解除する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    if not ctx.is_muted(guild.id, user.id):
        await interaction.response.send_message(
            f"{user.display_name} はミュートされていません"
        )
        return
    await ctx.remove_mute(guild.id, user.id)
    await interaction.response.send_message(
        f"{user.display_name} のミュートを解除しました"
    )


async def showmute(ctx: Any, interaction: discord.Interaction) -> None:
    """現在の guild のミュート中ユーザーを一覧する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    mutes = ctx.guild_mutes.get(guild.id, set())
    if not mutes:
        await interaction.response.send_message("ミュート中のユーザーはいません")
        return
    lines: list[str] = []
    for uid in mutes:
        member = guild.get_member(uid)
        name = member.display_name if member else f"ID: {uid}"
        lines.append(f"  {name}")
    await interaction.response.send_message(
        f"ミュート中（{len(mutes)}人）\n" + "\n".join(lines)
    )


async def speaker(
    ctx: Any,
    interaction: discord.Interaction,
    character: str,
    style: str | None = None,
) -> None:
    """実行者の話者をキャラクターと任意のスタイルで設定する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    if not ctx.characters and not ctx.speaker_engine:
        await ctx._refresh_speakers_if_needed()
    elif ctx.characters:
        await ctx._refresh_missing_speakers_if_needed()

    if not ctx.characters:
        await interaction.response.send_message(
            "スピーカー情報がまだ読み込まれていません"
        )
        return

    matched_char = match_speaker_character(ctx, character)
    if not matched_char:
        await interaction.response.send_message(
            f"キャラクター「{character}」が見つかりません"
        )
        return

    styles = ctx.characters[matched_char]
    matched_style = match_speaker_style(styles, style)
    if not matched_style:
        style_names = ", ".join(s[1] for s in styles)
        await interaction.response.send_message(
            f"「{matched_char}」に"
            f"スタイル「{style}」がありません\n"
            f"利用可能: {style_names}"
        )
        return

    speaker_id = matched_style[0]
    settings = ctx.get_user_settings(guild.id, interaction.user.id)
    settings = ctx.VoiceSettings(
        speaker_id=speaker_id,
        speed=settings.speed,
        pitch=settings.pitch,
        intonation=settings.intonation,
        volume=settings.volume,
    )
    ctx.user_settings[(guild.id, interaction.user.id)] = settings
    await ctx.save_user_setting(guild.id, interaction.user.id, settings)
    name = ctx.speakers_cache.get(speaker_id, f"ID: {speaker_id}")
    await interaction.response.send_message(f"キャラクターを「{name}」に変更しました")


def match_speaker_character(ctx: Any, character: str) -> str | None:
    """話者キャラクターを完全一致、次に部分一致で探す。"""
    query = character.strip().lower()
    if not query:
        return None

    partial: str | None = None
    for char_name in ctx.characters:
        lowered_key = char_name.lower()
        if query == lowered_key:
            return char_name
        if partial is None and query in lowered_key:
            partial = char_name
    return partial


def match_speaker_style(
    styles: list[tuple[int, str]], style: str | None
) -> tuple[int, str] | None:
    """話者スタイルを完全一致、次に部分一致で探す。"""
    if not styles:
        return None
    if style is None or not style.strip():
        return styles[0]

    partial: tuple[int, str] | None = None
    style_query = style.lower()
    for global_id, style_name in styles:
        if style_query == style_name.lower():
            return (global_id, style_name)
        if partial is None and style_query in style_name.lower():
            partial = (global_id, style_name)
    return partial


def interaction_option_value(
    interaction: discord.Interaction, option_name: str
) -> str | None:
    """Discord autocomplete interaction から raw option 値を読む。"""
    data: Mapping[str, object]
    if isinstance(interaction.data, dict):
        data = cast(Mapping[str, object], interaction.data)
    else:
        data = {}

    options: object = data.get("options", [])
    if not isinstance(options, list):
        return None
    options_list = cast(list[object], options)
    for opt in options_list:
        if not isinstance(opt, dict):
            continue
        option = cast(Mapping[str, object], opt)
        if option.get("name") == option_name:
            value = option.get("value", "")
            return value if isinstance(value, str) else str(value)
    return None


async def speaker_char_autocomplete(
    ctx: Any, interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """autocomplete 入力に一致するキャラクター候補を最大 25 件返す。"""
    _ = interaction
    if not ctx.characters and not ctx.speaker_engine:
        ctx._spawn_background(ctx._refresh_speakers_if_needed())
    elif ctx.characters:
        ctx._schedule_missing_speaker_refresh()

    if not ctx.characters:
        return []

    query = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for char_name in ctx.characters:
        if current == "" or query in char_name.lower():
            choices.append(app_commands.Choice(name=char_name, value=char_name))
            if len(choices) >= 25:
                break
    return choices


async def speaker_style_autocomplete(
    ctx: Any, interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """現在選択中のキャラクター option に対応するスタイル候補を返す。"""
    if ctx.characters:
        ctx._schedule_missing_speaker_refresh()

    char_input = interaction_option_value(interaction, "character")
    if not char_input or not ctx.characters:
        return []

    matched_char = match_speaker_character(ctx, char_input)
    if not matched_char:
        return []

    styles = ctx.characters[matched_char]
    choices: list[app_commands.Choice[str]] = []
    for _, style_name in styles:
        if current == "" or current.lower() in style_name.lower():
            choices.append(app_commands.Choice(name=style_name, value=style_name))
            if len(choices) >= 25:
                break
    return choices


async def voice(
    ctx: Any,
    interaction: discord.Interaction,
    speed: float | None = None,
    pitch: float | None = None,
    intonation: float | None = None,
    volume: float | None = None,
) -> None:
    """実行者の音声合成パラメータを表示または更新する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    settings = ctx.get_user_settings(guild.id, interaction.user.id)

    new_speed = settings.speed if speed is None else max(0.5, min(2.0, speed))
    new_pitch = settings.pitch if pitch is None else max(-0.15, min(0.15, pitch))
    new_intonation = (
        settings.intonation if intonation is None else max(0.0, min(2.0, intonation))
    )
    new_volume = settings.volume if volume is None else max(0.0, min(2.0, volume))

    if speed is None and pitch is None and intonation is None and volume is None:
        speaker_name = ctx.speakers_cache.get(
            settings.speaker_id, f"ID: {settings.speaker_id}"
        )
        await interaction.response.send_message(
            f"現在の音声設定:\n"
            f"  キャラクター: {speaker_name}\n"
            f"  話速: {settings.speed}\n"
            f"  音高: {settings.pitch}\n"
            f"  抑揚: {settings.intonation}\n"
            f"  音量: {settings.volume}"
        )
        return

    new_settings = ctx.VoiceSettings(
        speaker_id=settings.speaker_id,
        speed=new_speed,
        pitch=new_pitch,
        intonation=new_intonation,
        volume=new_volume,
    )
    ctx.user_settings[(guild.id, interaction.user.id)] = new_settings
    await ctx.save_user_setting(guild.id, interaction.user.id, new_settings)

    changed: list[str] = []
    if speed is not None:
        changed.append(f"話速: {new_speed}")
    if pitch is not None:
        changed.append(f"音高: {new_pitch}")
    if intonation is not None:
        changed.append(f"抑揚: {new_intonation}")
    if volume is not None:
        changed.append(f"音量: {new_volume}")

    await interaction.response.send_message(
        "音声設定を変更しました\n  " + "\n  ".join(changed)
    )


async def dictionary(ctx: Any, interaction: discord.Interaction) -> None:
    """guild 辞書管理 UI を開く。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return

    content, view = ctx.build_dict_message(guild.id)
    await interaction.response.send_message(content=content, view=view)


async def panel(ctx: Any, interaction: discord.Interaction) -> None:
    """一般的な Bot 操作用の公開 control panel を再投稿する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    trace_id = ctx._new_trace_id()
    ctx._log_event(
        logging.INFO,
        "command.panel",
        trace_id=trace_id,
        guild_id=guild.id,
        user_id=interaction.user.id,
    )
    await interaction.response.send_message(
        embed=ctx._build_panel_embed(guild),
        view=ctx.ControlPanelView(guild),
    )


async def status(
    ctx: Any, interaction: discord.Interaction, private: bool = True
) -> None:
    """接続、queue、話者、engine 状態を表示する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    trace_id = ctx._new_trace_id()
    ctx._log_event(
        logging.INFO,
        "command.status",
        trace_id=trace_id,
        guild_id=guild.id,
        user_id=interaction.user.id,
    )
    await interaction.response.send_message(
        embed=ctx._build_status_embed(guild),
        ephemeral=private,
    )


async def license(ctx: Any, interaction: discord.Interaction) -> None:
    """license link と現在話者の credit 案内を private に表示する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    await interaction.response.send_message(
        embed=ctx._build_license_embed(
            guild_id=guild.id,
            user_id=interaction.user.id,
        ),
        ephemeral=True,
    )


async def credit(ctx: Any, interaction: discord.Interaction) -> None:
    """現在話者の credit 候補を private に表示する。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    await interaction.response.send_message(
        embed=ctx._build_license_embed(guild_id=guild.id, user_id=interaction.user.id),
        ephemeral=True,
    )


async def help_command(ctx: Any, interaction: discord.Interaction) -> None:
    """旧 help 導線からも統合 panel を表示するための互換 handler。"""
    guild = await ctx._require_guild_interaction(interaction)
    if guild is None:
        return
    await interaction.response.send_message(
        embed=ctx._build_panel_embed(guild),
        view=ctx.ControlPanelView(guild),
    )
