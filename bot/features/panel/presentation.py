"""control-panel feature 用の Discord presentation helper。"""

import discord

from .models import PanelSnapshot


def build_panel_embed(
    snapshot: PanelSnapshot, *, notice: str | None = None
) -> discord.Embed:
    """``/panel``、接続完了、refresh 操作で使う統合 panel embed を作る。

    引数:
        snapshot: Discord adapter が組み立てた公開用 panel state。
        notice: 接続完了など、その投稿だけに先頭表示する短い案内。

    戻り値:
        ユーザー向けの接続数、コマンド、ライセンス案内を含む Discord embed。
    """
    embed = discord.Embed(
        title="読み上げBot 操作パネル",
        description=notice,
        color=0x22C55E if snapshot.process_voice_connection_count > 0 else 0x94A3B8,
    )
    embed.add_field(
        name="接続数",
        value=f"{snapshot.process_voice_connection_count} VC",
        inline=False,
    )
    embed.add_field(
        name="コマンド",
        value=(
            "`/vc` — 接続と切断をまとめて切り替え\n"
            "`/panel` — このパネルをもう一度投稿\n"
            "`/mute` `/unmute` `/showmute` — 読み上げミュートの管理"
        ),
        inline=False,
    )
    embed.add_field(
        name="音声とライセンス",
        value=(
            "\n".join(snapshot.license_lines)
            if snapshot.license_lines
            else "パネルの「ライセンス」から確認できます"
        ),
        inline=False,
    )
    embed.set_footer(text="いま見ているパネルは「更新」で最新表示にできます")
    return embed
