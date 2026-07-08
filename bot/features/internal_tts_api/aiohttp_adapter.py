"""internal TTS API feature 用の aiohttp adapter。"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping
from typing import Protocol, cast

from aiohttp import web

type NumericPayloadValue = str | int | float


class VoiceSettingsFactory(Protocol):
    """合成 use case が受け取る音声設定を作る factory。"""

    def __call__(
        self,
        *,
        speaker_id: int = ...,
        speed: float = ...,
        pitch: float = ...,
        intonation: float = ...,
        volume: float = ...,
    ) -> object: ...


class InternalTtsApiContext(Protocol):
    """internal TTS API adapter が必要とする runtime surface。"""

    # この Protocol は aiohttp adapter が Bot 全体へ要求する最小 interface。
    # ここに Discord の View/Command や DB pool を足したくなったら、
    # internal API feature の責務が広がりすぎていないかを先に疑う。
    BOT_INSTANCE_INDEX: str
    DEFAULT_SPEAKER: int
    INTERNAL_TTS_API_ENABLED: bool
    INTERNAL_TTS_API_HOST: str
    INTERNAL_TTS_API_MAX_TEXT_LENGTH: int
    INTERNAL_TTS_API_PORT: int
    INTERNAL_TTS_API_TOKEN: str
    IS_MULTIBOT_CHILD: bool
    VoiceSettings: VoiceSettingsFactory
    logger: logging.Logger

    def apply_dict(self, guild_id: int, text: str) -> str: ...

    def apply_reading_corrections(self, text: str) -> str: ...

    def clean_text(self, text: str) -> str: ...

    def get_internal_tts_api_runner(self) -> web.AppRunner | None: ...

    async def synthesize(
        self, text: str, settings: object, cache: bool = False
    ) -> bytes: ...

    def set_internal_tts_api_runner(self, runner: web.AppRunner | None) -> None: ...


def _numeric_payload_value(raw_value: object, name: str) -> NumericPayloadValue:
    """数値へ変換できる JSON payload 値を返す。"""
    # bool は Python では int のサブクラスだが、JSON API の利用者にとって
    # true/false が speaker_id=1/0 になるのは直感に反するので明示的に拒否する。
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
        raise ValueError(f"{name} must be a number")
    return raw_value


def should_start(ctx: InternalTtsApiContext) -> bool:
    """この process で internal API を起動すべきかを返す。"""
    if not ctx.INTERNAL_TTS_API_ENABLED:
        return False
    if not ctx.INTERNAL_TTS_API_TOKEN:
        ctx.logger.warning(
            "内部TTS APIは有効ですが INTERNAL_TTS_API_TOKEN が未設定です"
        )
        return False
    return not ctx.IS_MULTIBOT_CHILD or ctx.BOT_INSTANCE_INDEX == "1"


def authorized(ctx: InternalTtsApiContext, request: web.Request) -> bool:
    """request の bearer token が設定値と一致するかを返す。"""
    if not ctx.INTERNAL_TTS_API_TOKEN:
        return False
    authorization = request.headers.get("Authorization", "")
    return hmac.compare_digest(authorization, f"Bearer {ctx.INTERNAL_TTS_API_TOKEN}")


def payload_float(
    payload: Mapping[str, object],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """float の payload 値を 1 件読み取り、検証する。"""
    raw_value = _numeric_payload_value(payload.get(name, default), name)
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def payload_int(
    payload: Mapping[str, object],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """integer の payload 値を 1 件読み取り、検証する。"""
    raw_value = _numeric_payload_value(payload.get(name, default), name)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


async def health(_request: web.Request) -> web.Response:
    """最小限の health response を返す。"""
    return web.json_response({"ok": True})


def prepare_text(ctx: InternalTtsApiContext, text: str, guild_id: int | None) -> str:
    """合成前の読み上げ pipeline と同じ手順で text を正規化する。"""
    text = ctx.clean_text(text)
    if guild_id is not None:
        text = ctx.apply_dict(guild_id, text)
    return ctx.apply_reading_corrections(text)


async def synthesize(ctx: InternalTtsApiContext, request: web.Request) -> web.Response:
    """internal synthesis endpoint を処理する。"""
    if not authorized(ctx, request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        raw_payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(raw_payload, Mapping):
        return web.json_response({"error": "payload_must_be_object"}, status=400)
    # aiohttp の json() は Any を返す。ここで「object の Mapping」として受け直し、
    # 各フィールドは payload_* helper で個別に検証する。
    payload = cast(Mapping[str, object], raw_payload)

    text = str(payload.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "text_required"}, status=400)
    if len(text) > ctx.INTERNAL_TTS_API_MAX_TEXT_LENGTH:
        return web.json_response({"error": "text_too_long"}, status=413)

    try:
        guild_id = (
            payload_int(payload, "guild_id", 0, 0, 18_446_744_073_709_551_615)
            if "guild_id" in payload
            else None
        )
        settings = ctx.VoiceSettings(
            speaker_id=payload_int(
                payload, "speaker_id", ctx.DEFAULT_SPEAKER, 0, 99999
            ),
            speed=payload_float(payload, "speed", 1.0, 0.5, 2.0),
            pitch=payload_float(payload, "pitch", 0.0, -0.15, 0.15),
            intonation=payload_float(payload, "intonation", 1.0, 0.0, 2.0),
            volume=payload_float(payload, "volume", 1.0, 0.0, 2.0),
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    try:
        text = prepare_text(ctx, text, guild_id)
        audio_data = await ctx.synthesize(
            text,
            settings,
            cache=bool(payload.get("cache", False)),
        )
    except Exception:
        # API 利用者へ内部例外や trace を返さない。詳細は運営側ログにだけ残す。
        ctx.logger.exception("内部TTS APIの合成に失敗しました")
        return web.json_response({"error": "synthesis_failed"}, status=502)

    return web.Response(
        body=audio_data,
        content_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


async def start(ctx: InternalTtsApiContext) -> None:
    """internal aiohttp TTS API server を起動する。"""
    if not should_start(ctx):
        return
    if ctx.get_internal_tts_api_runner() is not None:
        return

    app = web.Application(client_max_size=16 * 1024)

    async def synthesize_handler(request: web.Request) -> web.Response:
        """runtime context を aiohttp request 1 件に束縛する。"""
        return await synthesize(ctx, request)

    app.router.add_get("/healthz", health)
    app.router.add_post("/synthesize", synthesize_handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=ctx.INTERNAL_TTS_API_HOST,
        port=ctx.INTERNAL_TTS_API_PORT,
    )
    try:
        await site.start()
    except Exception:
        # runner.setup() 後の失敗では cleanup しないと socket/handler が残る。
        await runner.cleanup()
        raise
    ctx.set_internal_tts_api_runner(runner)
    ctx.logger.info(
        "内部TTS APIを起動しました: host=%s port=%s auth=%s",
        ctx.INTERNAL_TTS_API_HOST,
        ctx.INTERNAL_TTS_API_PORT,
        "enabled" if ctx.INTERNAL_TTS_API_TOKEN else "disabled",
    )


async def stop(ctx: InternalTtsApiContext) -> None:
    """internal aiohttp TTS API server を停止する。"""
    runner = ctx.get_internal_tts_api_runner()
    if runner is None:
        return
    ctx.set_internal_tts_api_runner(None)
    await runner.cleanup()
