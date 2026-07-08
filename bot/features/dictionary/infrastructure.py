"""dictionary feature の database access。

ここには dictionary が所有する table だけを置く。対象は guild 辞書と
built-in 読み override。connection-pool 作成は ``app.database`` が所有する。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

from . import application


async def ensure_schema(conn: Any) -> None:
    """dictionary が所有する table がなければ作成する。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_dicts (
            guild_id BIGINT NOT NULL,
            word TEXT NOT NULL,
            reading TEXT NOT NULL,
            PRIMARY KEY (guild_id, word)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS builtin_reading_dicts (
            dict_type TEXT NOT NULL,
            word TEXT NOT NULL,
            reading TEXT NOT NULL,
            PRIMARY KEY (dict_type, word),
            CHECK (dict_type IN ('jp', 'en'))
        )
    """)


async def load_guild_dicts(
    pool: Any,
    guild_dicts: MutableMapping[int, dict[str, str]],
    *,
    to_katakana: Callable[[str], str],
    logger: Any,
) -> None:
    """guild 辞書をメモリへ読み込み、置換 cache を消す。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, word, reading FROM guild_dicts")
    guild_dicts.clear()
    application.clear_cache()
    for row in rows:
        gid = row["guild_id"]
        if gid not in guild_dicts:
            guild_dicts[gid] = {}
        # 古い行にはひらがなが残っていることがあるため、
        # VOICEVOX is_kana mode 向けに正規化する。
        guild_dicts[gid][row["word"]] = to_katakana(row["reading"])
    logger.info(f"辞書設定を読み込みました: {len(guild_dicts)}ギルド")


async def load_builtin_reading_dicts(
    pool: Any,
    *,
    reading_corrections: MutableMapping[str, str],
    english_word_readings: MutableMapping[str, str],
    default_reading_corrections: Mapping[str, str],
    default_english_word_readings: Mapping[str, str],
    to_katakana: Callable[[str], str],
    rebuild_reading_patterns: Callable[[], None],
    logger: Any,
) -> None:
    """built-in 読み override を読み込み、不足している bundled default を seed する。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT dict_type, word, reading FROM builtin_reading_dicts"
        )

        db_jp = {
            row["word"]: to_katakana(row["reading"])
            for row in rows
            if row["dict_type"] == "jp"
        }
        db_en = {
            row["word"]: to_katakana(row["reading"])
            for row in rows
            if row["dict_type"] == "en"
        }

        missing_seed_rows = [
            ("jp", word, reading)
            for word, reading in default_reading_corrections.items()
            if word not in db_jp
        ] + [
            ("en", word, reading)
            for word, reading in default_english_word_readings.items()
            if word not in db_en
        ]
        if missing_seed_rows:
            await conn.executemany(
                """
                INSERT INTO builtin_reading_dicts (dict_type, word, reading)
                VALUES ($1, $2, $3)
                ON CONFLICT (dict_type, word) DO NOTHING
                """,
                missing_seed_rows,
            )

    jp = dict(default_reading_corrections)
    jp.update(db_jp)
    en = dict(default_english_word_readings)
    en.update(db_en)

    reading_corrections.clear()
    reading_corrections.update(jp)
    english_word_readings.clear()
    english_word_readings.update(en)
    rebuild_reading_patterns()

    if missing_seed_rows:
        logger.info(
            "built-in読み辞書をDBへ不足分投入しました: "
            f"inserted={len(missing_seed_rows)}件, "
            f"jp={len(reading_corrections)}件, en={len(english_word_readings)}件"
        )
    else:
        logger.info(
            "built-in読み辞書をDBから読み込みました: "
            f"jp={len(reading_corrections)}件, en={len(english_word_readings)}件"
        )


async def add_dict_entry(
    pool: Any,
    guild_dicts: MutableMapping[int, dict[str, str]],
    *,
    guild_id: int,
    word: str,
    reading: str,
    reading_corrections: Mapping[str, str],
    english_word_readings: Mapping[str, str],
    to_katakana: Callable[[str], str],
) -> bool:
    """built-in と重複しない場合だけ guild 辞書行を 1 件保存する。"""
    normalized_reading = to_katakana(reading)
    if application.is_builtin_duplicate(
        word, normalized_reading, reading_corrections, english_word_readings
    ):
        return False

    # DB I/O を await する前にメモリを更新し、
    # reader が古い pattern を使わないようにする。
    guild_dicts.setdefault(guild_id, {})[word] = normalized_reading
    application.invalidate_cache(guild_id)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guild_dicts (guild_id, word, reading)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, word) DO UPDATE SET reading = $3
            """,
            guild_id,
            word,
            normalized_reading,
        )
    return True


async def delete_dict_entry(
    pool: Any,
    guild_dicts: MutableMapping[int, dict[str, str]],
    *,
    guild_id: int,
    word: str,
) -> None:
    """guild 辞書行をメモリと DB から 1 件削除する。"""
    dictionary = guild_dicts.get(guild_id)
    if dictionary is not None:
        dictionary.pop(word, None)
        if not dictionary:
            guild_dicts.pop(guild_id, None)
    application.invalidate_cache(guild_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_dicts WHERE guild_id = $1 AND word = $2",
            guild_id,
            word,
        )


async def purge_builtin_duplicates_from_user_dicts(
    pool: Any,
    guild_dicts: MutableMapping[int, dict[str, str]],
    *,
    reading_corrections: Mapping[str, str],
    english_word_readings: Mapping[str, str],
    logger: Any,
) -> int:
    """built-in で完全にカバーされる guild 辞書行を削除する。"""
    pairs_to_delete: list[tuple[int, str]] = []
    per_guild_count: dict[int, int] = {}
    for gid, dictionary in guild_dicts.items():
        for word, reading in dictionary.items():
            if application.is_builtin_duplicate(
                word, reading, reading_corrections, english_word_readings
            ):
                pairs_to_delete.append((gid, word))
                per_guild_count[gid] = per_guild_count.get(gid, 0) + 1
    if not pairs_to_delete:
        return 0

    for gid, word in pairs_to_delete:
        dictionary = guild_dicts.get(gid)
        if dictionary:
            dictionary.pop(word, None)
            if not dictionary:
                guild_dicts.pop(gid, None)
    for gid in per_guild_count:
        application.invalidate_cache(gid)

    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                "DELETE FROM guild_dicts WHERE guild_id = $1 AND word = $2",
                pairs_to_delete,
            )
    except Exception as e:
        logger.warning(
            f"ビルドイン重複ユーザー辞書のDB削除に失敗: {e} "
            f"(メモリは {len(pairs_to_delete)} 件削除済み、次起動時に再試行)"
        )
        return len(pairs_to_delete)

    breakdown = ", ".join(
        f"guild={gid}:{count}件" for gid, count in sorted(per_guild_count.items())
    )
    logger.info(
        f"ビルドインと重複するユーザー辞書を {len(pairs_to_delete)} 件削除 "
        f"({breakdown})"
    )
    return len(pairs_to_delete)
