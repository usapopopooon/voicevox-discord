"""status feature 用の Discord presentation helper。"""

import discord

from .models import StatusSnapshot


def _format_names(names: tuple[str, ...], *, empty: str) -> str:
    """embed field 用の短いカンマ区切り list を整形する。

    引数:
        names: 表示する engine 名または item 名。
        empty: ``names`` が空の場合に表示する text。

    戻り値:
        Discord embed field 向けの短い表示文字列。
    """
    return ", ".join(names) if names else empty


def build_status_embed(snapshot: StatusSnapshot) -> discord.Embed:
    """公開用 status embed を作る。

    引数:
        snapshot: Discord adapter が組み立てた公開用 status state。

    戻り値:
        trace ID、DB state、process ID、生 guild ID などの内部 debug data を
        意図的に含めない Discord embed。
    """
    read_target = (
        f"<#{snapshot.read_channel_id}>"
        if snapshot.read_channel_id is not None
        else "未設定"
    )
    embed = discord.Embed(
        title="読み上げBot ステータス",
        color=0x22C55E if snapshot.connected else 0x94A3B8,
    )
    embed.add_field(
        name="接続",
        value=(
            f"状態: {'接続中' if snapshot.connected else '未接続'}\n"
            f"VC: {snapshot.voice_channel_name}\n"
            f"読み上げ対象: {read_target}"
        ),
        inline=False,
    )
    embed.add_field(
        name="キュー",
        value=f"{snapshot.queue_length} / {snapshot.queue_maxlen}",
        inline=True,
    )
    embed.add_field(
        name="話者",
        value=f"{snapshot.speaker_count} 件読み込み済み",
        inline=True,
    )
    embed.add_field(
        name="エンジン",
        value=(
            f"設定: {_format_names(snapshot.configured_engines, empty='なし')}\n"
            f"取得成功: {_format_names(snapshot.healthy_engines, empty='未取得')}"
        ),
        inline=False,
    )
    embed.set_footer(text="状態は実際の接続状況により変わる場合があります。")
    return embed
