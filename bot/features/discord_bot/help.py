"""discord_bot feature の help embed presentation。"""

from __future__ import annotations

import discord

from features import license as license_feature

VOICEVOX_OFFICIAL_URL = "https://voicevox.hiroshiba.jp/"
COEIROINK_OFFICIAL_URL = "https://coeiroink.com/"
SHAREVOX_OFFICIAL_URL = "https://sharevox.app/"


def build_help_embed(prefix: str | None = None) -> discord.Embed:
    """旧 help 導線向けに、統合 panel の要点を説明する embed を作る。"""
    body = (
        "読み上げBotの操作は `/panel` に集約しています。\n"
        "接続、切断、スキップ、話者変更、音声設定、辞書、状態確認、"
        "ライセンス確認はパネルのボタンから操作できます。\n\n"
        "コマンド:\n"
        "`/vc` — 接続と切断をまとめて切り替え\n"
        "`/panel` — 操作パネルをもう一度投稿\n"
        "`/mute` — 読み上げをミュート\n"
        "`/unmute` — 読み上げミュートを解除\n"
        "`/showmute` — ミュート中のユーザーを確認\n\n"
        "各ボイスおよびライセンスはこちら:\n"
        + "\n".join(
            f"{info.engine}: {info.official_url}"
            for info in license_feature.LICENSE_INFOS
        )
    )
    description = f"{prefix}\n\n{body}" if prefix else body
    return discord.Embed(
        title="読み上げBot — パネル案内",
        description=description,
        color=0x00B0F4,
    )
