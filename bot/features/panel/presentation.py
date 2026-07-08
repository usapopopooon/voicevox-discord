"""control-panel feature 用の Discord presentation helper。"""

import discord

from .models import PanelSnapshot


def build_panel_embed(snapshot: PanelSnapshot) -> discord.Embed:
    """``/panel`` と refresh 操作で使う embed を作る。

    引数:
        snapshot: Discord adapter が組み立てた公開用 panel state。

    戻り値:
        ユーザー向けの運用状態だけを含む Discord embed。
    """
    read_target = (
        f"<#{snapshot.read_channel_id}>"
        if snapshot.read_channel_id is not None
        else "未設定"
    )
    playback_state = (
        "再生中" if snapshot.playing else "待機中" if snapshot.connected else "未接続"
    )
    embed = discord.Embed(
        title="読み上げBot 操作パネル",
        description="接続、読み上げ設定、辞書、状態確認をまとめて操作できます。",
        color=0x22C55E if snapshot.connected else 0x94A3B8,
    )
    embed.add_field(
        name="現在の状態",
        value=(
            f"VC: {snapshot.voice_channel_name}\n"
            f"読み上げ対象: {read_target}\n"
            f"キュー: {snapshot.queue_length} / {snapshot.queue_maxlen}\n"
            f"接続: {'接続中' if snapshot.connected else '未接続'}\n"
            f"再生: {playback_state}"
        ),
        inline=False,
    )
    embed.set_footer(text="状態が変わったら「更新」で最新表示にできます")
    return embed
