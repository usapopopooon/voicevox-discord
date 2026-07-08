"""voice-synthesis feature 用の外部 TTS engine access。

この file は意図的に HTTP と engine payload だけを扱う。
Bot adapter や Discord object をこの layer に入れない。
"""

from __future__ import annotations

from typing import Any

from .application import VoiceSynthesisSettings

DISCORD_OUTPUT_SAMPLE_RATE = 48000


async def fetch_speakers(ctx: Any) -> None:
    """設定済み engine すべてから話者を取得して runtime cache へ入れる。"""
    ctx.speakers_cache.clear()
    ctx.speaker_engine.clear()
    ctx.characters.clear()
    ctx._speaker_fetch_success_engines.clear()

    session = await ctx.get_http_session()
    for engine_name, engine_url, offset in ctx.ENGINES:
        try:
            async with session.get(
                f"{engine_url}/speakers",
                timeout=ctx.aiohttp.ClientTimeout(
                    total=ctx.TTS_SPEAKERS_TIMEOUT_SECONDS
                ),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            count = 0
            for speaker in data:
                char_name = speaker["name"]
                char_key = (
                    f"[{engine_name}] {char_name}"
                    if len(ctx.ENGINES) > 1
                    else char_name
                )
                if char_key not in ctx.characters:
                    ctx.characters[char_key] = []
                for style in speaker["styles"]:
                    real_id = style["id"]
                    global_id = real_id + offset
                    style_name = style["name"]
                    ctx.speakers_cache[global_id] = f"{char_key}（{style_name}）"
                    ctx.speaker_engine[global_id] = (engine_url, real_id)
                    ctx.characters[char_key].append((global_id, style_name))
                    count += 1

            ctx._speaker_fetch_success_engines.add(engine_name)
            ctx.logger.info(f"スピーカー取得成功: {engine_name} ({count}件)")
        except Exception as e:
            ctx.logger.warning(f"スピーカー取得失敗: {engine_name}: {e}")

    ctx.logger.info(f"スピーカー一覧合計: {len(ctx.speakers_cache)}件")


async def synthesize_with_candidate(
    ctx: Any,
    engine_url: str,
    real_id: int,
    text: str,
    settings: VoiceSynthesisSettings,
) -> bytes:
    """engine 候補 1 件に対して audio_query + synthesis request を実行する。"""
    session = await ctx.get_http_session()
    params = {"text": text, "speaker": real_id}
    async with session.post(
        f"{engine_url}/audio_query",
        params=params,
        timeout=ctx.aiohttp.ClientTimeout(total=ctx.TTS_AUDIO_QUERY_TIMEOUT_SECONDS),
    ) as resp:
        resp.raise_for_status()
        query = await resp.json()

    query["speedScale"] = settings.speed
    query["pitchScale"] = settings.pitch
    query["intonationScale"] = settings.intonation
    query["volumeScale"] = settings.volume
    query["outputSamplingRate"] = DISCORD_OUTPUT_SAMPLE_RATE
    query["outputStereo"] = True

    async with session.post(
        f"{engine_url}/synthesis",
        params={"speaker": real_id},
        json=query,
        headers={"Content-Type": "application/json"},
        timeout=ctx.aiohttp.ClientTimeout(total=ctx.TTS_SYNTHESIS_TIMEOUT_SECONDS),
    ) as resp:
        resp.raise_for_status()
        return await resp.read()
