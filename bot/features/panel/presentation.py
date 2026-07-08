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
        ユーザー向けの状態、操作入口、残している command を含む Discord embed。
    """
    read_target = (
        f"<#{snapshot.read_channel_id}>"
        if snapshot.read_channel_id is not None
        else "未設定"
    )
    playback_state = (
        "再生中" if snapshot.playing else "待機中" if snapshot.connected else "未接続"
    )
    description_lines = [
        "読み上げの操作と案内をまとめたパネルです。",
        "接続後も必要になったら `/panel` で同じパネルを再投稿できます。",
    ]
    if notice:
        description_lines.insert(0, notice)
    embed = discord.Embed(
        title="読み上げBot 操作パネル",
        description="\n".join(description_lines),
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
    embed.add_field(
        name="パネルでできること",
        value=(
            "接続 / 切断 / スキップ\n"
            "話者変更 / 音声設定 / 辞書管理\n"
            "状態確認 / ライセンス確認 / 新しいパネル投稿"
        ),
        inline=False,
    )
    embed.add_field(
        name="残しているコマンド",
        value=(
            "`/vc` — VC接続/切断をトグル\n"
            "`/panel` — このパネルを再投稿\n"
            "`/mute` `/unmute` `/showmute` — ミュート管理"
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
