"""Bot 本体に依存しない音声合成ユースケースとキャッシュ処理。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp


class VoiceSynthesisSettings(Protocol):
    """音声合成 feature が必要とする音声パラメータの形。"""

    speaker_id: int
    speed: float
    pitch: float
    intonation: float
    volume: float


type SynthCacheKey = tuple[str, int, str, float, float, float, float, int]


@dataclass(frozen=True)
class SynthCandidate:
    """グローバル話者 ID から解決した音声合成候補。"""

    engine_url: str
    real_id: int
    reason: str


def prune_candidate_fail_until(ctx: Any) -> None:
    """期限切れの合成候補バックオフ情報を削除する。"""
    now = ctx.time.monotonic()
    expired = [
        key for key, deadline in ctx._candidate_fail_until.items() if deadline <= now
    ]
    for key in expired:
        ctx._candidate_fail_until.pop(key, None)


def has_missing_configured_speaker_engines(ctx: Any) -> bool:
    """設定済みエンジンのうち話者情報を未取得のものがあるかを返す。"""
    configured = {name for name, _, _ in ctx.ENGINES}
    return bool(configured) and not configured.issubset(
        ctx._speaker_fetch_success_engines
    )


async def refresh_speakers_if_needed(ctx: Any) -> None:
    """短い間引き間隔を挟みながら話者情報を再取得する。"""
    now = ctx.time.monotonic()
    if now - ctx._last_speaker_refresh_attempt < ctx.SPEAKER_REFRESH_INTERVAL:
        return

    async with ctx._speaker_refresh_lock:
        now = ctx.time.monotonic()
        if now - ctx._last_speaker_refresh_attempt < ctx.SPEAKER_REFRESH_INTERVAL:
            return
        ctx._last_speaker_refresh_attempt = now
        await ctx.fetch_speakers()


async def refresh_missing_speakers_if_needed(ctx: Any) -> None:
    """未取得のエンジンがある場合だけ話者情報を再取得する。"""
    if ctx._has_missing_configured_speaker_engines():
        await ctx._refresh_speakers_if_needed()


def schedule_missing_speaker_refresh(ctx: Any) -> None:
    """UI を待たせずに話者情報の再取得をバックグラウンド予約する。"""
    if ctx._has_missing_configured_speaker_engines():
        ctx._spawn_background(ctx._refresh_speakers_if_needed())


def synth_cache_key(
    candidate: SynthCandidate, text: str, settings: VoiceSynthesisSettings
) -> SynthCacheKey:
    """音声合成キャッシュ用の安定したキーを組み立てる。"""
    return (
        candidate.engine_url,
        candidate.real_id,
        text,
        settings.speed,
        settings.pitch,
        settings.intonation,
        settings.volume,
        settings.speaker_id,
    )


def lookup_synth_cache(
    ctx: Any,
    candidates: list[SynthCandidate],
    text: str,
    settings: VoiceSynthesisSettings,
) -> bytes | None:
    """候補のいずれかで LRU キャッシュが当たれば返す。"""
    # requested speaker の mapping が起動後に更新されることがあるため、
    # primary candidate だけではなく fallback candidate の key も見る。
    for candidate in candidates:
        key = ctx._synth_cache_key(candidate, text, settings)
        cached = ctx._synth_cache.get(key)
        if cached is not None:
            ctx._synth_cache.move_to_end(key)
            return cached
    return None


def lookup_recent_synth_cache(
    ctx: Any,
    candidates: list[SynthCandidate],
    text: str,
    settings: VoiceSynthesisSettings,
) -> bytes | None:
    """候補のいずれかで短期キャッシュが当たれば返す。"""
    now = ctx.time.monotonic()
    for candidate in candidates:
        key = ctx._synth_cache_key(candidate, text, settings)
        entry = ctx._recent_synth_cache.get(key)
        if entry is None:
            continue
        expires_at, data = entry
        if expires_at <= now:
            ctx._recent_synth_cache.pop(key, None)
            continue
        ctx._recent_synth_cache.move_to_end(key)
        return data
    return None


def store_synth_cache(
    ctx: Any,
    primary_key: SynthCacheKey,
    actual_key: SynthCacheKey,
    data: bytes,
) -> None:
    """合成済みバイト列を実際の候補キーと最優先候補キーへ保存する。"""
    # actual_key: 実際に成功した candidate。
    # primary_key: 呼び出し時点で最優先の candidate。
    # fallback で成功した結果を primary_key にも置くと、同じリクエストが次回すぐ返る。
    ctx._synth_cache[actual_key] = data
    ctx._synth_cache.move_to_end(actual_key)
    if actual_key != primary_key:
        ctx._synth_cache[primary_key] = data
        ctx._synth_cache.move_to_end(primary_key)
    while len(ctx._synth_cache) > ctx._SYNTH_CACHE_MAX:
        ctx._synth_cache.popitem(last=False)


def store_recent_synth_cache(ctx: Any, key: SynthCacheKey, data: bytes) -> None:
    """合成済みバイト列を短期重複排除キャッシュへ保存する。"""
    ctx._recent_synth_cache[key] = (
        ctx.time.monotonic() + ctx._RECENT_SYNTH_TTL_SECONDS,
        data,
    )
    ctx._recent_synth_cache.move_to_end(key)
    while len(ctx._recent_synth_cache) > ctx._RECENT_SYNTH_CACHE_MAX:
        ctx._recent_synth_cache.popitem(last=False)


async def build_synthesis_candidates(
    ctx: Any, requested_speaker_id: int
) -> list[SynthCandidate]:
    """重複を除きながら優先順の音声合成候補を組み立てる。"""
    seen: set[tuple[str, int]] = set()
    candidates: list[SynthCandidate] = []

    def add(engine_url: str, real_id: int, reason: str) -> None:
        # 複数経路から同じ engine/speaker が候補に入ることがある。重複を残すと
        # 同じ失敗を連続で試すだけなので、順序は保ったまま一度だけ入れる。
        key = (engine_url, real_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append(SynthCandidate(engine_url, real_id, reason))

    if (info := ctx.speaker_engine.get(requested_speaker_id)) is not None:
        add(info[0], info[1], "requested_speaker")

    if not ctx.speaker_engine:
        # speaker metadata がまだ空なら、初回読み上げで一度だけ同期取得を試す。
        # 起動直後の race で「候補なし」になる体験を避けるため。
        await ctx._refresh_speakers_if_needed()
        if (info := ctx.speaker_engine.get(requested_speaker_id)) is not None:
            add(info[0], info[1], "requested_after_refresh")

    if (info := ctx.speaker_engine.get(ctx.DEFAULT_SPEAKER)) is not None:
        add(info[0], info[1], "default_speaker_mapping")

    # キャッシュ済み speaker を少数だけ fallback に入れる。大量に試すと障害時の
    # 待ち時間が伸びるため、ここでは「復旧可能性」と「遅延」のバランスを取る。
    for global_id in sorted(ctx.speaker_engine.keys())[:3]:
        info = ctx.speaker_engine[global_id]
        add(info[0], info[1], "cached_speaker_fallback")

    # metadata が取れていなくても、engine URL と raw speaker id だけで通る
    # エンジンがあるため最後の fallback として残す。
    for _, engine_url, _ in ctx.ENGINES:
        add(engine_url, ctx.DEFAULT_SPEAKER, "raw_default_id")

    return candidates


async def try_candidate(
    ctx: Any,
    candidate: SynthCandidate,
    text: str,
    settings: VoiceSynthesisSettings,
    primary_key: SynthCacheKey | None,
) -> bytes:
    """候補を 1 つ試し、キャッシュとバックオフ状態を更新する。"""
    pair = (candidate.engine_url, candidate.real_id)
    try:
        data = await ctx._synthesize_with_candidate(
            candidate.engine_url, candidate.real_id, text, settings
        )
    except (aiohttp.ClientError, TimeoutError):
        # ネットワーク/timeout 系だけ短期バックオフする。合成パラメータ不正などの
        # 論理エラーまで backoff すると原因調査が遅れるため。
        ctx._candidate_fail_until[pair] = (
            ctx.time.monotonic() + ctx.CANDIDATE_FAIL_BACKOFF_SECONDS
        )
        raise

    if ctx._candidate_fail_until.pop(pair, None) is not None:
        ctx.logger.info(
            "音声合成エンジン復旧: "
            f"engine={candidate.engine_url}, speaker={candidate.real_id}"
        )
    actual_key = ctx._synth_cache_key(candidate, text, settings)
    ctx._store_recent_synth_cache(actual_key, data)
    if primary_key is not None and actual_key != primary_key:
        # fallback 成功時も primary_key で待っていた同時リクエストを救う。
        ctx._store_recent_synth_cache(primary_key, data)
    if primary_key is not None:
        ctx._store_synth_cache(primary_key, actual_key, data)
    return data


async def run_candidates(
    ctx: Any,
    candidates: list[SynthCandidate],
    text: str,
    settings: VoiceSynthesisSettings,
    primary_key: SynthCacheKey | None,
) -> bytes:
    """候補を順に試し、最初に成功した合成結果を返す。"""
    ctx._prune_candidate_fail_until()
    last_error: Exception | None = None
    attempted = False
    now = ctx.time.monotonic()

    for idx, candidate in enumerate(candidates):
        if (
            ctx._candidate_fail_until.get(
                (candidate.engine_url, candidate.real_id), 0.0
            )
            > now
        ):
            # 直近で落ちた候補はスキップし、次の候補に早く進む。
            continue
        attempted = True
        try:
            data = await ctx._try_candidate(candidate, text, settings, primary_key)
            if idx > 0:
                ctx.logger.warning(
                    f"音声合成フォールバック成功: reason={candidate.reason}, "
                    f"engine={candidate.engine_url}, speaker={candidate.real_id}"
                )
            return data
        except (aiohttp.ClientError, TimeoutError) as e:
            last_error = e
            ctx.logger.warning(
                f"音声合成候補失敗: reason={candidate.reason}, "
                f"engine={candidate.engine_url}, speaker={candidate.real_id}, "
                f"error={e}"
            )

    if not attempted:
        raise aiohttp.ClientConnectionError("音声合成候補はバックオフ中です")
    if last_error is not None:
        raise last_error
    raise RuntimeError("音声合成に失敗しました（全フォールバック候補失敗）")


async def synthesize(
    ctx: Any, text: str, settings: VoiceSynthesisSettings, cache: bool = False
) -> bytes:
    """フォールバック候補とキャッシュを使ってテキストを WAV バイト列へ合成する。"""
    trace_id = ctx._new_trace_id()
    started = ctx.time.monotonic()
    ctx._log_event(
        logging.DEBUG,
        "tts.synthesize.start",
        trace_id=trace_id,
        speaker_id=settings.speaker_id,
        text_length=len(text),
        cache=cache,
    )
    if not ctx.ENGINES:
        raise RuntimeError("TTSエンジンが設定されていません")

    candidates = await ctx._build_synthesis_candidates(settings.speaker_id)
    if not candidates:
        raise RuntimeError("音声合成候補がありません。エンジン設定を確認してください")

    primary_key = ctx._synth_cache_key(candidates[0], text, settings)

    if not cache:
        # 通常読み上げは長期 LRU には入れない。ただし同時投稿/短時間重複を避けるため、
        # recent TTL と in-flight だけは使う。
        recent = ctx._lookup_recent_synth_cache(candidates, text, settings)
        if recent is not None:
            ctx._log_event(
                logging.DEBUG,
                "tts.synthesize.cache_hit",
                trace_id=trace_id,
                speaker_id=settings.speaker_id,
                cache_type="recent",
                latency_ms=round((ctx.time.monotonic() - started) * 1000, 2),
            )
            return recent

        in_flight = ctx._synth_in_flight.get(primary_key)
        if in_flight is not None:
            # 同じ key の先行合成が走っている場合は待つ。待機後に recent cache を
            # 見ることで、同じ HTTP request を重複発行しない。
            await in_flight.wait()
            recent = ctx._lookup_recent_synth_cache(candidates, text, settings)
            if recent is not None:
                ctx._log_event(
                    logging.DEBUG,
                    "tts.synthesize.cache_hit",
                    trace_id=trace_id,
                    speaker_id=settings.speaker_id,
                    cache_type="recent_after_wait",
                    latency_ms=round((ctx.time.monotonic() - started) * 1000, 2),
                )
                return recent
        in_flight_event = ctx.asyncio.Event()
        ctx._synth_in_flight[primary_key] = in_flight_event
        try:
            result = await ctx._run_candidates(
                candidates, text, settings, primary_key=None
            )
            _log_synthesis_success(ctx, trace_id, started, settings, text, candidates)
            return result
        except Exception as e:
            _log_synthesis_failure(
                ctx, trace_id, started, settings, text, candidates, e
            )
            raise
        finally:
            # 成功/失敗のどちらでも待機者を起こす。失敗時も Event を set しないと、
            # 後続の同 key リクエストが永久に待つ。
            ctx._synth_in_flight.pop(primary_key, None)
            in_flight_event.set()

    cached = ctx._lookup_synth_cache(candidates, text, settings)
    if cached is not None:
        ctx._log_event(
            logging.DEBUG,
            "tts.synthesize.cache_hit",
            trace_id=trace_id,
            speaker_id=settings.speaker_id,
            cache_type="lru",
            latency_ms=round((ctx.time.monotonic() - started) * 1000, 2),
        )
        return cached

    in_flight = ctx._synth_in_flight.get(primary_key)
    if in_flight is not None:
        # cache=True は長期 LRU を使うため、先行合成の完了後は LRU を見る。
        await in_flight.wait()
        cached = ctx._lookup_synth_cache(candidates, text, settings)
        if cached is not None:
            ctx._log_event(
                logging.DEBUG,
                "tts.synthesize.cache_hit",
                trace_id=trace_id,
                speaker_id=settings.speaker_id,
                cache_type="lru_after_wait",
                latency_ms=round((ctx.time.monotonic() - started) * 1000, 2),
            )
            return cached

    in_flight_event = ctx.asyncio.Event()
    ctx._synth_in_flight[primary_key] = in_flight_event
    try:
        result = await ctx._run_candidates(candidates, text, settings, primary_key)
        _log_synthesis_success(ctx, trace_id, started, settings, text, candidates)
        return result
    except Exception as e:
        if ctx._has_missing_configured_speaker_engines():
            ctx._schedule_missing_speaker_refresh()
        _log_synthesis_failure(ctx, trace_id, started, settings, text, candidates, e)
        raise
    finally:
        # cache=True 側も必ず待機者を起こす。上の non-cache branch と同じ安全装置。
        ctx._synth_in_flight.pop(primary_key, None)
        in_flight_event.set()


def _log_synthesis_success(
    ctx: Any,
    trace_id: str,
    started: float,
    settings: VoiceSynthesisSettings,
    text: str,
    candidates: list[SynthCandidate],
) -> None:
    """音声合成リクエスト 1 件分の成功ログを構造化して出力する。"""
    ctx._log_event(
        logging.INFO,
        "tts.synthesize.succeeded",
        trace_id=trace_id,
        speaker_id=settings.speaker_id,
        text_length=len(text),
        candidate_count=len(candidates),
        latency_ms=round((ctx.time.monotonic() - started) * 1000, 2),
    )


def _log_synthesis_failure(
    ctx: Any,
    trace_id: str,
    started: float,
    settings: VoiceSynthesisSettings,
    text: str,
    candidates: list[SynthCandidate],
    error: Exception,
) -> None:
    """音声合成リクエスト 1 件分の失敗状態を構造化して記録する。"""
    ctx._record_recent_error("tts.synthesize.failed", str(error), trace_id)
    ctx._log_event(
        logging.WARNING,
        "tts.synthesize.failed",
        trace_id=trace_id,
        speaker_id=settings.speaker_id,
        text_length=len(text),
        candidate_count=len(candidates),
        latency_ms=round((ctx.time.monotonic() - started) * 1000, 2),
        error=str(error),
    )
