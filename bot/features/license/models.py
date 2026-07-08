"""license feature 内で使う data model。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LicenseInfo:
    """ユーザーに表示する静的な license metadata。

    属性:
        engine: TTS engine の表示名。
        official_url: 公式 product または project page。
        terms_url: ユーザーが確認すべき規約または license page。
        credit_hint: 想定される credit 表記の短い例。
        note: license 項目に併記する人間向けの注意書き。
    """

    engine: str
    official_url: str
    terms_url: str
    credit_hint: str
    note: str


@dataclass(frozen=True)
class CurrentCredit:
    """現在のユーザーが選択している話者の credit 候補。

    属性:
        speaker_name: 内部 engine prefix を除いた話者表示名。
        credit: 推奨 credit 文字列。Bot が識別できる場合は解決済み engine 名も含める。
    """

    speaker_name: str
    credit: str
