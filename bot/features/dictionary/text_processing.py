"""dictionary feature の text / reading 正規化。

この module は合成前に使う純粋な text preprocessing pipeline を所有する。
対象は URL/email/custom emoji の cleanup、顔文字正規化、ネット slang、
title 読み、Unicode emoji 読み、built-in の漢字/英単語読み補正。
runtime DB code は export された辞書を変更してから pattern を再構築できるため、
既存 call site と test 向けに Bot adapter はまだこれらの名前を re-export する。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from features import dictionary as dictionary_feature

try:
    import emoji as emoji_lib
except ImportError:  # pragma: no cover
    emoji_lib = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextProcessingRuntimeState:
    """text processing 抽出中に ``bot.py`` と同期する state。

    この形は意図的に明示的で単純にしている。後で TypeScript へ移植する場合は
    interface にそのまま写しやすく、composition boundary で stringly-typed な
    ``getattr`` / ``setattr`` 同期を広げずに済む。
    """

    kaomoji_dict: dict[str, str]
    kaomoji_pattern: re.Pattern[str] | None
    reading_corrections: dict[str, str]
    english_word_readings: dict[str, str]
    default_reading_corrections: Mapping[str, str]
    default_english_word_readings: Mapping[str, str]
    reading_pattern: re.Pattern[str] | None
    english_word_pattern: re.Pattern[str] | None


# テキスト前処理用の正規表現（1パスで URL / メール / カスタム絵文字を置換）
_CLEAN_TEXT_PATTERN = re.compile(
    r"(?P<url>https?://\S+)"
    r"|(?P<email>[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"
    r"|<a?:(?P<emoji>\w+):\d+>"
)
# 既存 import 互換のため個別パターンも残す
URL_PATTERN = re.compile(r"https?://\S+")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w+):\d+>")
MAX_READ_LENGTH = 100


def _clean_text_replace(m: re.Match[str]) -> str:
    if m.group("url") is not None:
        return "URLしょうりゃく"
    if m.group("email") is not None:
        return "メールアドレスしょうりゃく"
    return m.group("emoji")  # カスタム絵文字は名前だけ残す


# kaomoji.json に含まれない素朴な基本形を補完する curated dict。
# Wikipedia の顔文字ページや一般的な日本語利用で頻出するものを中心に選定。
_BASIC_KAOMOJI: dict[str, str] = {
    # 笑顔・喜び
    "(^_^)": "にっこり",
    "(^-^)": "にっこり",
    "(^o^)": "わーい",
    "(^○^)": "にっこり",
    "(^∇^)": "にっこり",
    "(^∀^)": "にっこり",
    "(^ω^)": "にっこり",
    "(*^_^*)": "にこにこ",
    "(*^-^*)": "にこにこ",
    "(*^o^*)": "わーい",
    "(#^^#)": "にこにこ",
    "(*´ω`*)": "にこにこ",
    "(*´∀`*)": "にこにこ",
    "(´∀`)": "にっこり",
    "(＾ω＾)": "にっこり",
    "(・∀・)": "にっこり",
    "＼(^o^)／": "ばんざい",
    "٩(ˊᗜˋ*)و": "わーい",
    # 困惑・汗
    "(^_^;)": "あせ",
    "(^^;)": "あせ",
    "(´∀`;)": "あせ",
    "(;・∀・)": "あせあせ",
    "(;´Д`)": "あせあせ",
    # 悲しい・泣く
    "(;_;)": "なく",
    "(T_T)": "なく",
    "(T-T)": "なく",
    "(ToT)": "なく",
    "(ToT)/~~~": "なく",
    "(´;ω;`)": "なく",
    "( ;∀;)": "なく",
    "(´Д⊂ヽ": "なく",
    "(>_<)": "つらい",
    "(つд⊂)": "なく",
    "(´；ω；｀)": "なく",
    "(；ω；)": "なく",
    # 驚き
    "Σ(ﾟдﾟ)": "びっくり",
    "(ﾟДﾟ)": "びっくり",
    "(ﾟдﾟ)": "びっくり",
    "Σ(°Д°;)": "びっくり",
    "Σ(´∀`;)": "びっくり",
    "(@_@)": "くらくら",
    "(*_*)": "くらくら",
    # 怒り
    "(#゚Д゚)": "おこる",
    "(-_-#)": "おこる",
    "(ಠ_ಠ)": "じとー",
    "(｀・ω・´)": "しゃきーん",
    "(｀・∀・´)": "しゃきーん",
    "(๑•̀ㅂ•́)و✧": "がんばる",
    # お辞儀・謝る
    "m(_ _)m": "ぺこり",
    "m(__)m": "ぺこり",
    "m(。_。)m": "ぺこり",
    # 眠い・ぼー
    "(ー_ー)": "じとー",
    "(-_-)": "うーん",
    "( ˘ω˘)": "ねむい",
    "(_ _)": "ねむい",
    "zzz": "ねむい",
    # 落ち込む・困る
    "(´・ω・`)": "しょぼーん",
    "( ´・ω・`)": "うーん",
    "(´・ω・)": "しょぼーん",
    "(・ω・)": "しょぼーん",
    "(*ﾉωﾉ)": "てれ",
    "( ˙꒳˙ )": "ふむ",
    "(´_ゝ`)": "ふーん",
    "(´Д`)": "はぁ",
    "¯\\_(ツ)_/¯": "やれやれ",
    "orz": "がっくり",
    "OTZ": "がっくり",
    "OTL": "がっくり",
    # 笑い（ネットスラング）
    "(笑)": "わらい",
    "(爆)": "ばくしょう",
    "(苦笑)": "にがわらい",
    # 挨拶・手振り
    "(^_^)/": "ばいばい",
    "ノシ": "ばいばい",
    "(´◡`)": "にっこり",
}

# 横向き（Western）顔文字は「語中誤爆」を避けるため境界付きで別処理する。
_WESTERN_EMOTICON_READING: dict[str, str] = {
    ":-)": "にっこり",
    ":)": "にっこり",
    ":-D": "えがお",
    ":D": "えがお",
    ";-)": "ういんく",
    ";)": "ういんく",
    ":-P": "てへぺろ",
    ":P": "てへぺろ",
    ":-(": "しょんぼり",
    ":(": "しょんぼり",
    ":'-(": "なく",
    ":'(": "なく",
    "XD": "おおわらい",
    "xD": "おおわらい",
    "<3": "はーと",
}
_WESTERN_EMOTICON_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<emo>"
    + "|".join(
        re.escape(k) for k in sorted(_WESTERN_EMOTICON_READING, key=len, reverse=True)
    )
    + r")"
    r"(?![A-Za-z0-9])"
)


# 顔文字辞書（ビルドイン）。
# もともと kaomoji.json で管理していた辞書をコード内に同梱し、
# 起動時ファイルI/Oをなくしてデプロイ環境依存を減らす。
# 組み込み辞書はカテゴリ別の静的データとして扱い、実行時に追加する基本形は
# text_processing 専用のコピーへ混ぜる。こうしておくと、カテゴリファイルの責務が
# 実行時状態に汚染されず、別言語へ移植する時も data と runtime state を分けやすい。
_KAOMOJI_DICT: dict[str, str] = dict(dictionary_feature.KAOMOJI_DICT)
# curated な基本形を優先（上書き可能）。ビルドイン辞書側の long-form と
# 重複しにくいシンプル形を中心に登録している。
_KAOMOJI_DICT.update(_BASIC_KAOMOJI)

_KAOMOJI_CHAR_NORMALIZE_MAP = str.maketrans(
    {
        "•": "・",
        "∙": "・",
        "·": "・",
        "⋅": "・",
        "˙": "・",
        "˘": "・",
        "〜": "~",
        "～": "~",
        "―": "-",
        "‐": "-",
        "−": "-",
    }
)


def _normalize_kaomoji_for_lookup(text: str) -> str:
    """顔文字照合用に表記ゆれ（全半角・類似記号）を正規化する。"""
    return unicodedata.normalize("NFKC", text).translate(_KAOMOJI_CHAR_NORMALIZE_MAP)


_KAOMOJI_NORMALIZED_DICT: dict[str, str] = {}
_kaomoji_normalized_max_len = 0
_kaomoji_pattern: re.Pattern[str] | None = None


def _rebuild_kaomoji_patterns() -> None:
    """_KAOMOJI_DICT の変更を反映して派生 pattern/normalized を再構築する。

    現状は起動時 1 回しか呼ばれないが、将来 DB 連動で kaomoji を動的追加する時に
    備えて一元化しておく。

    `_KAOMOJI_NORMALIZED_DICT` には「正規化で表記が変わるキー」だけを登録する。
    正規化結果が元キーと同一なエントリは `_replace_kaomoji` で `_KAOMOJI_PATTERN`
    経由で先に拾われるので、別 dict に重複保持する必要がない（数千件分の節約）。
    """
    global _kaomoji_normalized_max_len, _kaomoji_pattern

    _KAOMOJI_NORMALIZED_DICT.clear()
    for face, reading in _KAOMOJI_DICT.items():
        normalized = _normalize_kaomoji_for_lookup(face)
        if normalized == face:
            continue
        _KAOMOJI_NORMALIZED_DICT.setdefault(normalized, reading)
    _kaomoji_normalized_max_len = max(
        (len(k) for k in _KAOMOJI_NORMALIZED_DICT), default=0
    )
    if _KAOMOJI_DICT:
        _kaomoji_pattern = re.compile(
            "|".join(re.escape(k) for k in sorted(_KAOMOJI_DICT, key=len, reverse=True))
        )
    else:
        _kaomoji_pattern = None
    globals()["_KAOMOJI_NORMALIZED_MAX_LEN"] = _kaomoji_normalized_max_len
    globals()["_KAOMOJI_PATTERN"] = _kaomoji_pattern


_rebuild_kaomoji_patterns()
_KAOMOJI_NORMALIZED_MAX_LEN = _kaomoji_normalized_max_len
_KAOMOJI_PATTERN = _kaomoji_pattern
if _KAOMOJI_DICT:
    logger.info(f"顔文字辞書を読み込みました: {len(_KAOMOJI_DICT)}件")
_KAOMOJI_OPENERS = {"(", "（"}
_KAOMOJI_CLOSERS = {")", "）"}
_WESTERN_EMOTICON_TRIGGER_CHARS = {":", ";", "=", "8", "x", "X"}
_JP_NET_SLANG_TRIGGER_CHARS = {"w", "W", "ｗ", "Ｗ"}


def _contains_any_char(text: str, candidates: set[str]) -> bool:
    """text 内に候補文字のいずれかが含まれるかを返す。"""
    return any(ch in candidates for ch in text)


def _replace_kaomoji(text: str) -> str:
    """顔文字を annotation（読み仮名）に置換する。辞書が無ければそのまま返す。"""
    if _kaomoji_pattern is None:
        return text
    return _kaomoji_pattern.sub(lambda m: _KAOMOJI_DICT[m.group(0)], text)


def _replace_kaomoji_variant(text: str) -> str:
    """顔文字の表記ゆれ（全半角・類似記号）を吸収して置換する。

    入れ子括弧や複合顔文字も拾えるよう、開き括弧位置から最長一致で探索する。
    """
    if _kaomoji_normalized_max_len <= 0:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # 正規化しながら全 substring を見ると高コストなので、顔文字の開始に
        # なり得る文字に当たった時だけ longest-match 探索を行う。
        if text[i] not in _KAOMOJI_OPENERS:
            out.append(text[i])
            i += 1
            continue

        matched = False
        max_end = min(n, i + _kaomoji_normalized_max_len)
        for end in range(max_end, i + 1, -1):
            if text[end - 1] not in _KAOMOJI_CLOSERS:
                continue
            token = text[i:end]
            normalized = _normalize_kaomoji_for_lookup(token)
            reading = _KAOMOJI_NORMALIZED_DICT.get(normalized)
            if reading is None:
                continue
            out.append(reading)
            i = end
            matched = True
            break

        if not matched:
            out.append(text[i])
            i += 1

    return "".join(out)


def _replace_western_emoticon(text: str) -> str:
    """横向き顔文字を境界付きで置換する（語中の :D などは誤変換しない）。"""
    return _WESTERN_EMOTICON_PATTERN.sub(
        lambda m: _WESTERN_EMOTICON_READING[m.group("emo")], text
    )


# 日本語圏のネットスラング（笑い表現）。
# 「草」は文字としての意味（くさ）も多いため変換対象にせず、TTS の自然読み「くさ」に
# 任せる。曖昧性のない 2 文字以上の連続だけを置換する。半角/全角・大小文字すべて。
_JP_NET_SLANG_PATTERN = re.compile(r"(?<![A-Za-z0-9.])[wWｗＷ]{2,}(?![A-Za-z0-9.])")


def _replace_jp_net_slang(text: str) -> str:
    """日本語ネットスラング（www / ｗｗ / WWW / Ｗｗ など）を読み仮名に置換する。

    `w` 1 文字を `わら` に対応させ、`www` → `わらわらわら` のように回数を
    保存して読み上げる。
    """
    return _JP_NET_SLANG_PATTERN.sub(lambda m: "わら" * len(m.group(0)), text)


# 固定語のネットスラング辞書。ASCII キーは大文字小文字を区別せず、
# 単語境界 (`(?<![A-Za-z0-9])`/`(?![A-Za-z0-9])`) で英文中の偶然一致を避ける。
# 日本語混在キー (今北産業 等) は単語境界不要。
# キーはすべて小文字で保持し、置換時は match.group(0).lower() で照合する。
_NET_SLANG_DICT: dict[str, str] = {
    # 半角ASCII（大小不問）
    "kwsk": "くわしく",
    "ggrks": "ぐぐれかす",
    "wktk": "わくてか",
    "ktkr": "きたこれ",
    "gdgd": "ぐだぐだ",
    "gkbr": "がくがくぶるぶる",
    "thx": "さんくす",
    "plz": "ぷりーず",
    "pls": "ぷりーず",
    "orz": "がっくり",
    "otz": "がっくり",
    # 日本語/混在
    "今北産業": "いまきたさんぎょう",
    "うp": "アップ",
}

_NET_SLANG_ASCII_KEYS = (
    "kwsk",
    "ggrks",
    "wktk",
    "ktkr",
    "gdgd",
    "gkbr",
    "thx",
    "plz",
    "pls",
    "orz",
    "otz",
)
_NET_SLANG_JA_KEYS = ("今北産業", "うp")

_NET_SLANG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(_NET_SLANG_ASCII_KEYS)
    + r")(?![A-Za-z0-9])|"
    + "|".join(re.escape(k) for k in _NET_SLANG_JA_KEYS),
    re.IGNORECASE,
)


def _replace_net_slang_dict(text: str) -> str:
    """固定語スラングを読み仮名に置換する（kwsk → くわしく, 今北産業 → ... 等）。"""
    return _NET_SLANG_PATTERN.sub(
        lambda m: _NET_SLANG_DICT.get(m.group(0).lower(), m.group(0)),
        text,
    )


# ゲーム/アニメ等、TTS が誤読しがちな英語タイトル/略称の読み辞書。
# `_NET_SLANG_DICT` と同じく単語境界 (`(?<![A-Za-z0-9])`/`(?![A-Za-z0-9])`) で
# 英文中の偶然一致を防ぎ、大小文字どちらでもマッチする。値はカタカナ。
# キーはすべて小文字で保持し、置換時は match.group(0).lower() で照合する。
_TITLE_READING_DICT: dict[str, str] = {
    "fate": "フェイト",
    "fgo": "フェイトグランドオーダー",
    "bleach": "ブリーチ",
    "naruto": "ナルト",
    "pokemon": "ポケモン",
    "dbz": "ドラゴンボールゼット",
    "sao": "ソードアートオンライン",
    "hxh": "ハンターハンター",
    "fps": "エフピーエス",
    "rpg": "アールピージー",
    "mmo": "エムエムオー",
    "trpg": "ティーアールピージー",
    "moba": "モバ",
}

_TITLE_READING_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(_TITLE_READING_DICT) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _replace_title_readings(text: str) -> str:
    """ゲーム/アニメ等の英語タイトル/略称を読み仮名に置換する。"""
    return _TITLE_READING_PATTERN.sub(
        lambda m: _TITLE_READING_DICT.get(m.group(0).lower(), m.group(0)),
        text,
    )


# 高頻度 Unicode 絵文字の読み替え。必要最小限に絞ってコストを抑える。
_UNICODE_EMOJI_READING: dict[str, str] = {
    "☺️": "にっこり",
    "☺": "にっこり",
    "😀": "にっこり",
    "😁": "にっこり",
    "😂": "わらい",
    "🤣": "わらい",
    "😆": "わらい",
    "😊": "えがお",
    "😍": "はーと",
    "😘": "はーと",
    "🥰": "はーと",
    "😉": "ういんく",
    "🤔": "うーん",
    "😢": "なき",
    "😭": "なき",
    "😡": "おこ",
    "😱": "びっくり",
    "😴": "ねむい",
    "🥺": "うるうる",
    "🙏": "ぺこり",
    "🙇": "ぺこり",
    "👍": "ぐー",
    "👎": "だめ",
    "👏": "ぱちぱち",
    "🙌": "ばんざい",
    "💯": "ひゃくてん",
    "🔥": "あつい",
    "✨": "きらきら",
    "🎉": "おめでとう",
    "❤️": "はーと",
    "❤": "はーと",
    "💕": "はーと",
    "💖": "はーと",
    "💔": "しょっく",
    "💤": "ねむい",
}
_UNICODE_EMOJI_PATTERN: re.Pattern[str] | None = re.compile(
    "|".join(
        re.escape(k) for k in sorted(_UNICODE_EMOJI_READING, key=len, reverse=True)
    )
)
_EMOJI_SKIN_TONE_MODIFIER_PATTERN = re.compile(r"[\U0001F3FB-\U0001F3FF]")

# デコ系でよく使う装飾記号（非Emoji含む）
_DECO_SYMBOL_READING: dict[str, str] = {
    "♡": "はーと",
    "♥": "はーと",
    "❤": "はーと",
    "❣": "はーと",
    "❥": "はーと",
    "☆": "ほし",
    "★": "ほし",
    "✩": "ほし",
    "✪": "ほし",
    "✦": "きらきら",
    "✧": "きらきら",
    "✨": "きらきら",
    "❇": "きらきら",
    "❈": "きらきら",
    "♪": "おんぷ",
    "♫": "おんぷ",
    "♬": "おんぷ",
    "♩": "おんぷ",
    "❀": "はな",
    "✿": "はな",
    "❁": "はな",
    "❃": "はな",
    "❋": "はな",
}
_DECO_SYMBOL_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(_DECO_SYMBOL_READING, key=len, reverse=True))
)
_DECO_SYMBOL_TRIGGER_CHARS = set(_DECO_SYMBOL_READING.keys())

# demojize で得られる short code の代表的なデコ系読み
_EMOJI_SHORTCODE_READING: dict[str, str] = {
    "sparkles": "きらきら",
    "sparkling_heart": "はーと",
    "heart_decoration": "はーと",
    "heart_exclamation": "はーと",
    "two_hearts": "はーと",
    "revolving_hearts": "はーと",
    "growing_heart": "はーと",
    "white_heart": "はーと",
    "pink_heart": "はーと",
    "blue_heart": "はーと",
    "green_heart": "はーと",
    "yellow_heart": "はーと",
    "purple_heart": "はーと",
    "ribbon": "りぼん",
    "gift": "ぷれぜんと",
    "wrapped_gift": "ぷれぜんと",
    "party_popper": "おいわい",
    "confetti_ball": "おいわい",
    "balloon": "ふうせん",
    "cherry_blossom": "さくら",
    "bouquet": "はなたば",
    "musical_note": "おんぷ",
    "musical_notes": "おんぷ",
}

_EMOJI_SHORTCODE_KEYWORD_READING: dict[str, str] = {
    "heart": "はーと",
    "sparkle": "きらきら",
    "star": "ほし",
    "music": "おんぷ",
    "flower": "はな",
    "blossom": "はな",
    "ribbon": "りぼん",
    "gift": "ぷれぜんと",
    "party": "おいわい",
    "confetti": "おいわい",
}


def _replace_deco_symbols(text: str) -> str:
    """デコ記号を読み仮名に置換する。"""
    return _DECO_SYMBOL_PATTERN.sub(lambda m: _DECO_SYMBOL_READING[m.group(0)], text)


def _shortcode_to_reading(shortcode: str) -> str | None:
    """絵文字 short code から当てられる日本語読みを返す。"""
    if not shortcode:
        return None
    direct = _EMOJI_SHORTCODE_READING.get(shortcode)
    if direct is not None:
        return direct
    for keyword, reading in _EMOJI_SHORTCODE_KEYWORD_READING.items():
        if keyword in shortcode:
            return reading
    return None


def _replace_unicode_emoji(text: str) -> str:
    """Unicode絵文字を短い読み仮名へ置換する。未知の絵文字は読まない。"""
    if emoji_lib is not None:
        # クロージャ内では Optional の narrowing を維持できないためローカルに固定する。
        _lib = emoji_lib

        def _replace(chars: str, _data: Mapping[str, object]) -> str:
            # まず完全一致、次に肌色 modifier を落とした一致、最後に shortcode
            # keyword の推測へ進む。未知絵文字を無理に読むと不自然なので空文字にする。
            reading = _UNICODE_EMOJI_READING.get(chars)
            if reading is not None:
                return reading
            normalized_chars = _EMOJI_SKIN_TONE_MODIFIER_PATTERN.sub("", chars)
            reading = _UNICODE_EMOJI_READING.get(normalized_chars)
            if reading is not None:
                return reading
            shortcode = _lib.demojize(normalized_chars, delimiters=("", ""))
            shortcode = shortcode.replace(":", "")
            guessed = _shortcode_to_reading(shortcode)
            if guessed is not None:
                return guessed
            return ""

        return _lib.replace_emoji(text, _replace)

    if _UNICODE_EMOJI_PATTERN is None:
        return text
    return _UNICODE_EMOJI_PATTERN.sub(
        lambda m: _UNICODE_EMOJI_READING[m.group(0)], text
    )


def _contains_possible_unicode_emoji(text: str) -> bool:
    """Unicode絵文字らしき文字を含むかを軽量に判定する。"""
    for ch in text:
        cp = ord(ch)
        if ch in _UNICODE_EMOJI_READING:
            return True
        # variation selector-16（絵文字表現の指示）/ ZWJ（絵文字連結）
        if ch in ("\ufe0f", "\u200d"):
            return True
        # Supplementary Symbols / Emoticons / Pictographs / Transport 等
        if 0x1F000 <= cp <= 0x1FAFF:
            return True
        # その他の symbol / dingbat
        if 0x2600 <= cp <= 0x27BF:
            return True
        # Enclosed Alphanumeric Supplement（ⓘ 等を含む）
        if 0x1F100 <= cp <= 0x1F1FF:
            return True
        # Regional Indicator Symbols（国旗：🇯🇵 等）
        if 0x1F1E6 <= cp <= 0x1F1FF:
            return True
    return False


def _normalize_emoji_modifiers(text: str) -> str:
    """絵文字肌色修飾子を除去して読み上げノイズを抑える。"""
    return _EMOJI_SKIN_TONE_MODIFIER_PATTERN.sub("", text)


# 誤読されやすい漢字と英単語の built-in 読み補正辞書は
# features.dictionary.builtin_readings に分離。
# `/dict` でユーザが登録すると一覧が長くなるので、一般的なものは Bot 側で対応する。
# on_message では apply_dict（ユーザ辞書）の後に適用し、ユーザ辞書で上書き可能にする。
# ランタイムで DB から上書きできるよう、コピーを保持する。
_READING_CORRECTIONS: dict[str, str] = dict(dictionary_feature.READING_CORRECTIONS)
_ENGLISH_WORD_READINGS: dict[str, str] = dict(dictionary_feature.ENGLISH_WORD_READINGS)

# built-in 読み辞書のデフォルトスナップショットへの読み取り専用ビュー。
# DB初期投入やフォールバックで利用する（runtime dict の変更に影響されない）。
# `MappingProxyType` でラップしてあるため、誤って `_DEFAULT_*[k] = v` などの
# 書き込みを行うと TypeError になり、`builtin_readings` 本体への意図しない
# 副作用を防ぐ。dict コピーを増やさないので追加メモリ消費はほぼゼロ。
_DEFAULT_READING_CORRECTIONS: Mapping[str, str] = MappingProxyType(
    dictionary_feature.READING_CORRECTIONS
)
_DEFAULT_ENGLISH_WORD_READINGS: Mapping[str, str] = MappingProxyType(
    dictionary_feature.ENGLISH_WORD_READINGS
)


def _english_base_form(word: str) -> str | None:
    """規則活用の英単語を基底語へ寄せる（簡易）。"""
    if len(word) < 4:
        return None
    # 比較級・最上級: happier → happy / happiest → happy
    if word.endswith("ier") and len(word) >= 5:
        candidate = word[:-3] + "y"
        if candidate in _ENGLISH_WORD_READINGS:
            return candidate
    if word.endswith("iest") and len(word) >= 6:
        candidate = word[:-4] + "y"
        if candidate in _ENGLISH_WORD_READINGS:
            return candidate
    # 一般的な比較級・最上級: bigger → big / biggest → big
    if word.endswith("er") and len(word) >= 4:
        base = word[:-2]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("est") and len(word) >= 5:
        base = word[:-3]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("ies") and len(word) >= 5:
        return word[:-3] + "y"
    if word.endswith("ied") and len(word) >= 5:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) >= 6:
        base = word[:-3]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if (base + "e") in _ENGLISH_WORD_READINGS:
            return base + "e"
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("ed") and len(word) >= 5:
        base = word[:-2]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if (base + "e") in _ENGLISH_WORD_READINGS:
            return base + "e"
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("es") and len(word) >= 5:
        # washes → wash / goes → go / boxes → box 等
        base = word[:-2]
        if base in _ENGLISH_WORD_READINGS:
            return base
    if word.endswith("s") and len(word) >= 4:
        base = word[:-1]
        if base in _ENGLISH_WORD_READINGS:
            return base
    return None


def _replace_english_word_match(m: re.Match[str]) -> str:
    word = m.group(0)
    key = word.lower()
    reading = _ENGLISH_WORD_READINGS.get(key)
    if reading is not None:
        return reading
    base = _english_base_form(key)
    if base is None:
        return word
    return _ENGLISH_WORD_READINGS.get(base, word)


_reading_pattern: re.Pattern[str] | None = None
_english_word_pattern: re.Pattern[str] | None = None


def _rebuild_reading_patterns() -> None:
    """現在の built-in 読み辞書から正規表現を再構築する。"""
    global _reading_pattern, _english_word_pattern
    _reading_pattern = (
        re.compile(
            "|".join(
                re.escape(k)
                for k in sorted(_READING_CORRECTIONS, key=len, reverse=True)
            )
        )
        if _READING_CORRECTIONS
        else None
    )
    _english_word_pattern = (
        # 英単語トークン全体を対象にし、callback側で辞書一致/基底語推定を行う。
        # これにより swimming などの活用形も読み補正できる。
        re.compile(r"(?<![A-Za-z])[A-Za-z]+(?![A-Za-z])", flags=re.IGNORECASE)
        if _ENGLISH_WORD_READINGS
        else None
    )
    globals()["_READING_PATTERN"] = _reading_pattern
    globals()["_ENGLISH_WORD_PATTERN"] = _english_word_pattern


_ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")
_rebuild_reading_patterns()
_READING_PATTERN = _reading_pattern
_ENGLISH_WORD_PATTERN = _english_word_pattern


def rebuild_kaomoji_patterns() -> None:
    """顔文字 lookup index を再構築する公開 API。"""
    _rebuild_kaomoji_patterns()


def rebuild_reading_patterns() -> None:
    """読み補正 lookup index を再構築する公開 API。"""
    _rebuild_reading_patterns()


def export_runtime_state() -> TextProcessingRuntimeState:
    """名前付き DTO 経由で互換 state を export する。

    多数の module-level alias ではなく、意図的に明示 object として扱う。
    読みやすく、型チェックしやすく、Python の module mutation を残さずに
    TypeScript interface へ移しやすい。
    """
    return TextProcessingRuntimeState(
        kaomoji_dict=_KAOMOJI_DICT,
        kaomoji_pattern=_kaomoji_pattern,
        reading_corrections=_READING_CORRECTIONS,
        english_word_readings=_ENGLISH_WORD_READINGS,
        default_reading_corrections=_DEFAULT_READING_CORRECTIONS,
        default_english_word_readings=_DEFAULT_ENGLISH_WORD_READINGS,
        reading_pattern=_reading_pattern,
        english_word_pattern=_english_word_pattern,
    )


def import_runtime_state(state: TextProcessingRuntimeState) -> None:
    """composition root から互換 state を import する。

    新規 code では明示的な function argument か feature-owned repository を優先する。
    この bridge は package-by-feature 移行中に legacy ``bot.py`` alias を
    動かし続けるためだけにある。
    """
    global _kaomoji_pattern, _reading_pattern, _english_word_pattern

    globals()["_KAOMOJI_DICT"] = state.kaomoji_dict
    globals()["_READING_CORRECTIONS"] = state.reading_corrections
    globals()["_ENGLISH_WORD_READINGS"] = state.english_word_readings
    _kaomoji_pattern = state.kaomoji_pattern
    _reading_pattern = state.reading_pattern
    _english_word_pattern = state.english_word_pattern
    globals()["_KAOMOJI_PATTERN"] = _kaomoji_pattern
    globals()["_READING_PATTERN"] = _reading_pattern
    globals()["_ENGLISH_WORD_PATTERN"] = _english_word_pattern


def apply_reading_corrections(text: str) -> str:
    """誤読されやすい漢字を読み仮名に置換する（長一致優先）。"""
    if _reading_pattern is not None:
        text = _reading_pattern.sub(lambda m: _READING_CORRECTIONS[m.group(0)], text)
    if _english_word_pattern is not None and _ASCII_LETTER_PATTERN.search(text):
        text = _english_word_pattern.sub(_replace_english_word_match, text)
    return text


def clean_text(text: str) -> str:
    """読み上げ用にテキストを前処理する。
    顔文字（長一致優先）→ 横向き顔文字（境界付き）→ 日本語ネットスラング
    → デコ記号 → Unicode絵文字
    → 肌色修飾子正規化 → URL/メール/カスタム絵文字 の順で置換する。"""
    # 顔文字パターンは巨大なので、括弧が無い通常文では regex 走査を避ける。
    # 呼び出し順序は `_replace_kaomoji` → `_replace_kaomoji_variant` で固定。
    # `_KAOMOJI_NORMALIZED_DICT` は「正規化で表記が変わるキー」のみ保持する縮小
    # 版なので、生キー（normalized==face）の置換は前段の `_replace_kaomoji` に
    # 依存している。順序を入れ替えたり片方だけ呼ぶと拾えない顔文字が出る点に注意。
    if _contains_any_char(text, _KAOMOJI_OPENERS):
        text = _replace_kaomoji(text)
        text = _replace_kaomoji_variant(text)
    # ASCII 系の横向き顔文字や wwww は通常文にも出やすいので、trigger char が
    # ない時は regex を走らせない。読み上げパスの hot path を軽くするため。
    if _contains_any_char(text, _WESTERN_EMOTICON_TRIGGER_CHARS):
        text = _replace_western_emoticon(text)
    if _contains_any_char(text, _JP_NET_SLANG_TRIGGER_CHARS):
        text = _replace_jp_net_slang(text)
    text = _replace_net_slang_dict(text)
    text = _replace_title_readings(text)
    if _contains_any_char(text, _DECO_SYMBOL_TRIGGER_CHARS):
        text = _replace_deco_symbols(text)
    if _contains_possible_unicode_emoji(text):
        text = _replace_unicode_emoji(text)
    if _EMOJI_SKIN_TONE_MODIFIER_PATTERN.search(text):
        text = _normalize_emoji_modifiers(text)
    return _CLEAN_TEXT_PATTERN.sub(_clean_text_replace, text).strip()
