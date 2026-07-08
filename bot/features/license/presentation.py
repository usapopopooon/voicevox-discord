"""license feature 用の Discord presentation helper。"""

import discord

from .models import CurrentCredit, LicenseInfo

LICENSE_INFOS: tuple[LicenseInfo, ...] = (
    LicenseInfo(
        engine="VOICEVOX",
        official_url="https://voicevox.hiroshiba.jp/",
        terms_url="https://voicevox.hiroshiba.jp/term/",
        credit_hint="VOICEVOX: 話者名",
        note="ソフトウェア規約に加えて、各音声ライブラリの規約を確認してください。",
    ),
    LicenseInfo(
        engine="COEIROINK",
        official_url="https://coeiroink.com/",
        terms_url="https://coeiroink.com/terms",
        credit_hint="COEIROINK: 話者名",
        note="生成音声はクレジット表記が必要です。キャラクターごとの利用条件も確認してください。",
    ),
    LicenseInfo(
        engine="SHAREVOX",
        official_url="https://sharevox.app/",
        terms_url="https://sharevox.app/",
        credit_hint="SHAREVOX: 話者名",
        note="公式サイトの案内と話者ごとの条件を確認してください。",
    ),
)


def build_license_embed(current: CurrentCredit | None = None) -> discord.Embed:
    """公開用の license / credit 案内 embed を作る。

    引数:
        current: リクエスト者の現在の話者設定に対応する任意の credit 候補。

    戻り値:
        公式 URL、規約 link、任意の現在話者 credit 候補を含む Discord embed。
    """
    embed = discord.Embed(
        title="音声ライセンス / クレジット",
        description=(
            "各音声エンジン・話者の利用規約に従ってご利用ください。"
            "外部公開時も、利用者側で用途に応じた条件確認が必要です。"
        ),
        color=0xF59E0B,
    )
    for info in LICENSE_INFOS:
        embed.add_field(
            name=info.engine,
            value=(
                f"公式: {info.official_url}\n"
                f"規約: {info.terms_url}\n"
                f"表記例: `{info.credit_hint}`\n"
                f"{info.note}"
            ),
            inline=False,
        )
    if current is not None:
        embed.add_field(
            name="現在のあなたの設定",
            value=f"話者: {current.speaker_name}\nクレジット候補: `{current.credit}`",
            inline=False,
        )
    embed.set_footer(
        text="規約は変更される場合があります。公開前に公式情報を確認してください。"
    )
    return embed
