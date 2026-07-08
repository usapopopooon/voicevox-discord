"""dictionary feature の公開 API。

この feature は漢字読み補正、英単語読み、顔文字読みなどの built-in 読み resource を
所有する。runtime code はこの module 経由で import し、物理 file 名は内部詳細に留める。
"""

from .application import (
    DICT_PATTERN_CACHE,
    apply_dictionary,
    clear_cache,
    invalidate_cache,
    is_builtin_duplicate,
)
from .builtin_kaomoji import KAOMOJI_DICT
from .builtin_readings import (
    ENGLISH_WORD_READINGS,
    READING_CORRECTIONS,
    to_katakana,
)

__all__ = [
    "DICT_PATTERN_CACHE",
    "ENGLISH_WORD_READINGS",
    "KAOMOJI_DICT",
    "READING_CORRECTIONS",
    "apply_dictionary",
    "clear_cache",
    "invalidate_cache",
    "is_builtin_duplicate",
    "to_katakana",
]
