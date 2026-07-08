"""guild 辞書置換の application logic。

置換 cache の振る舞いは、汎用 persistence ではなく「辞書をどう適用するか」の一部なので
dictionary feature が所有する。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping

# 後方互換用 runtime state は ``bot.py`` から re-export されるが、
# 実際の owner はこの feature module。
DICT_PATTERN_CACHE: dict[int, re.Pattern[str]] = {}


def invalidate_cache(guild_id: int) -> None:
    """guild 1 件分の compiled replacement pattern を無効化する。"""
    DICT_PATTERN_CACHE.pop(guild_id, None)


def clear_cache() -> None:
    """compiled replacement pattern をすべて無効化する。"""
    DICT_PATTERN_CACHE.clear()


def is_builtin_duplicate(
    word: str,
    reading: str,
    reading_corrections: Mapping[str, str],
    english_word_readings: Mapping[str, str],
) -> bool:
    """ユーザー辞書行が built-in 行と完全重複するかを返す。

    引数:
        word: ユーザーが入力した辞書 key。
        reading: 正規化済みの読み値。
        reading_corrections: 日本語 built-in 読み。
        english_word_readings: lower-case key の英単語 built-in 読み。
    """
    if reading_corrections.get(word) == reading:
        return True
    if english_word_readings.get(word.lower()) == reading:
        return True
    return False


def apply_dictionary(
    guild_id: int,
    text: str,
    guild_dicts: Mapping[int, Mapping[str, str]],
    pattern_cache: MutableMapping[int, re.Pattern[str]] = DICT_PATTERN_CACHE,
) -> str:
    """guild 辞書 1 件を single-pass で適用する。

    single-pass replacement により、``a -> b`` と ``b -> c`` のような
    chained substitution が ``a`` を ``c`` に変えてしまう事故を防ぐ。
    """
    dictionary = guild_dicts.get(guild_id, {})
    if not dictionary:
        return text
    pattern = pattern_cache.get(guild_id)
    if pattern is None:
        words_sorted = sorted(dictionary.keys(), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(word) for word in words_sorted))
        pattern_cache[guild_id] = pattern
    return pattern.sub(lambda match: dictionary[match.group(0)], text)
