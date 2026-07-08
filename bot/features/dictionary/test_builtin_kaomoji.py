"""built-in 顔文字カテゴリ data の test。"""

from features.dictionary.builtin_kaomoji import KAOMOJI_DICT
from features.dictionary.builtin_kaomoji.actions import KAOMOJI_ACTIONS
from features.dictionary.builtin_kaomoji.negative import KAOMOJI_NEGATIVE
from features.dictionary.builtin_kaomoji.positive import KAOMOJI_POSITIVE
from features.dictionary.builtin_kaomoji.reactions import KAOMOJI_REACTIONS
from features.dictionary.builtin_kaomoji.slang import KAOMOJI_SLANG
from features.dictionary.builtin_kaomoji.social import KAOMOJI_SOCIAL

KAOMOJI_CATEGORIES = (
    KAOMOJI_SOCIAL,
    KAOMOJI_POSITIVE,
    KAOMOJI_NEGATIVE,
    KAOMOJI_ACTIONS,
    KAOMOJI_SLANG,
    KAOMOJI_REACTIONS,
)


def test_builtin_kaomoji_categories_are_disjoint() -> None:
    """各顔文字 entry が意味カテゴリ 1 つだけに属すること。"""
    total_entries = sum(len(category) for category in KAOMOJI_CATEGORIES)
    merged_keys = set().union(*(category.keys() for category in KAOMOJI_CATEGORIES))

    assert total_entries == len(merged_keys)
    assert len(KAOMOJI_DICT) == total_entries
    assert set(KAOMOJI_DICT) == merged_keys


def test_text_processing_does_not_mutate_builtin_kaomoji_data() -> None:
    """runtime の顔文字追加がカテゴリ別 source data を書き換えないこと。"""
    before_runtime_import = dict(KAOMOJI_DICT)

    from features.dictionary import text_processing

    assert KAOMOJI_DICT == before_runtime_import
    assert text_processing._KAOMOJI_DICT is not KAOMOJI_DICT
    assert len(text_processing._KAOMOJI_DICT) >= len(KAOMOJI_DICT)
