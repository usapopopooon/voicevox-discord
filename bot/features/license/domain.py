"""license domain の純粋 helper。

この module は意図的に Discord、DB、HTTP、環境変数へ依存しない。
plain string と小さな value object だけで test できる。
"""

import re

from .models import CurrentCredit

_ENGINE_PREFIX_PATTERN = re.compile(r"^\[[^\]]+\]\s*")


def normalize_speaker_display_name(speaker_name: str) -> str:
    """話者表示名から内部用の ``[ENGINE]`` prefix を取り除く。

    引数:
        speaker_name: ``speakers_cache`` 由来、または fallback の表示名。

    戻り値:
        credit 候補としてユーザーに見せる話者名。
    """
    return _ENGINE_PREFIX_PATTERN.sub("", speaker_name, count=1)


def engine_name_from_speaker_display_name(speaker_name: str) -> str | None:
    """prefix 付き話者表示名から engine 名を取り出す。

    引数:
        speaker_name: ``"[VOICEVOX] ずんだもん"`` のような表示名。

    戻り値:
        bracket を除いた engine 名。plain な名前なら ``None``。
    """
    match = _ENGINE_PREFIX_PATTERN.match(speaker_name)
    if match is None:
        return None
    return match.group(0).strip("[] ")


def credit_for_speaker(
    speaker_id: int,
    raw_speaker_name: str | None,
    *,
    engine_name: str | None = None,
) -> CurrentCredit:
    """話者の credit 候補を作る。

    引数:
        speaker_id: fallback 表示 label に使う数値の話者 ID。
        raw_speaker_name: runtime cache 由来の話者名。engine 識別用の内部
            ``[ENGINE]`` prefix を含むことがある。
        engine_name: runtime の話者 metadata から解決した engine。指定された場合は
            表示名 prefix より優先する。

    戻り値:
        Discord embed に安全に描画できる ``CurrentCredit`` 値。
    """
    display_name = raw_speaker_name or f"ID: {speaker_id}"
    speaker_name = normalize_speaker_display_name(display_name)
    resolved_engine_name = engine_name or engine_name_from_speaker_display_name(
        display_name
    )
    credit = (
        f"{resolved_engine_name}: {speaker_name}"
        if resolved_engine_name is not None
        else speaker_name
    )
    return CurrentCredit(speaker_name=speaker_name, credit=credit)
