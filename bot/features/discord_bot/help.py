"""discord_bot feature の help embed presentation。"""

from __future__ import annotations

import discord

from features import license as license_feature

VOICEVOX_OFFICIAL_URL = "https://voicevox.hiroshiba.jp/"
COEIROINK_OFFICIAL_URL = "https://coeiroink.com/"
SHAREVOX_OFFICIAL_URL = "https://sharevox.app/"


def build_help_embed(prefix: str | None = None) -> discord.Embed:
    """``/help`` と ``/join`` で表示する command list embed を作る。"""
    body = (
        "`/vc` — VCに接続/切断（トグル）\n"
        "`/panel` — 操作パネルを表示\n"
        "`/join` — VCに接続\n"
        "`/leave` — VCから切断\n"
        "`/skip` — 読み上げをスキップ\n"
        "`/speaker` — 読み上げキャラクター変更\n"
        "`/voice` — 話速・音高・抑揚・音量\n"
        "`/dict` — 読み上げ辞書の管理\n"
        "`/mute` — ユーザーをミュート\n"
        "`/unmute` — ミュート解除\n"
        "`/showmute` — ミュート一覧\n"
        "`/status` — 接続状態・キュー・エンジン状態を表示\n"
        "`/license` — 音声ライセンス/規約リンクを表示\n"
        "`/credit` — 現在の話者のクレジット候補を表示\n"
        "`/help` — このヘルプを表示\n\n"
        "各ボイスおよびライセンスはこちら:\n"
        + "\n".join(
            f"{info.engine}: {info.official_url}"
            for info in license_feature.LICENSE_INFOS
        )
    )
    description = f"{prefix}\n\n{body}" if prefix else body
    return discord.Embed(
        title="読み上げBot — コマンド一覧",
        description=description,
        color=0x00B0F4,
    )
