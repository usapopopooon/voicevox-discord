"""dictionary feature が使う built-in 顔文字読み。

raw mapping は file size ではなく、読み注釈の意味で分割する。
正規化や pattern 再構築などの振る舞いは ``text_processing.py`` に置く。
"""

from .actions import KAOMOJI_ACTIONS
from .negative import KAOMOJI_NEGATIVE
from .positive import KAOMOJI_POSITIVE
from .reactions import KAOMOJI_REACTIONS
from .slang import KAOMOJI_SLANG
from .social import KAOMOJI_SOCIAL

# category の所有が diff review で分かるよう、merge は明示的な順序で書く。
# category dict は互いに disjoint である前提で、test がそれを守る。
KAOMOJI_DICT: dict[str, str] = {
    **KAOMOJI_SOCIAL,
    **KAOMOJI_POSITIVE,
    **KAOMOJI_NEGATIVE,
    **KAOMOJI_ACTIONS,
    **KAOMOJI_SLANG,
    **KAOMOJI_REACTIONS,
}

__all__ = [
    "KAOMOJI_ACTIONS",
    "KAOMOJI_DICT",
    "KAOMOJI_NEGATIVE",
    "KAOMOJI_POSITIVE",
    "KAOMOJI_REACTIONS",
    "KAOMOJI_SLANG",
    "KAOMOJI_SOCIAL",
]
