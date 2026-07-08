"""起動設定を解決する helper。"""

from __future__ import annotations

import re
from typing import Protocol, overload


class EnvironmentReader(Protocol):
    """起動設定が使う小さな環境変数 API。

    この feature が必要とする Python の ``os`` module の一部だけを表し、
    TypeScript 化時は ``ConfigSource`` interface へ素直に写せる形にしている。
    """

    @overload
    def getenv(self, key: str) -> str | None: ...

    @overload
    def getenv(self, key: str, default: str) -> str: ...


class ConfigContext(Protocol):
    """起動設定の解決に必要な runtime 値。"""

    DISCORD_TOKEN: str
    DISCORD_TOKENS_RAW: str
    os: EnvironmentReader


def env_flag(ctx: ConfigContext, name: str, default: bool = False) -> bool:
    """環境変数を真偽値 flag として解釈する。"""
    raw = ctx.os.getenv(name)
    if raw is None:
        return default
    # 環境変数は Coolify/Docker/GitHub Actions で表記が揺れやすいので、
    # よく使う true 系だけを許可し、それ以外は False として扱う。
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_discord_token(token: str) -> str:
    """token の空白と任意の囲み quote を正規化する。"""
    normalized = token.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {'"', "'"}
    ):
        normalized = normalized[1:-1].strip()
    return normalized


def resolve_discord_tokens(ctx: ConfigContext) -> list[str]:
    """DISCORD_TOKENS / DISCORD_TOKEN から重複のない token list を作る。"""
    tokens: list[str] = []
    if ctx.DISCORD_TOKENS_RAW.strip():
        # 複数トークン運用では、改行・空白・半角/全角カンマが混ざりやすい。
        # ここでまとめて正規化し、launcher 側は単純な list として扱えるようにする。
        token_source = ctx.DISCORD_TOKENS_RAW.replace("，", ",")
        tokens.extend(
            token for token in re.split(r"[\s,]+", token_source.strip()) if token
        )
    if ctx.DISCORD_TOKEN.strip():
        tokens.append(ctx.DISCORD_TOKEN.strip())

    unique_tokens: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = normalize_discord_token(token)
        if not normalized or normalized in seen:
            continue
        # 同じトークンを重複起動すると Discord 側で session conflict になるため、
        # 順序を保ったまま重複排除する。
        seen.add(normalized)
        unique_tokens.append(normalized)
    return unique_tokens


def compose_profile_enabled(ctx: ConfigContext, profile: str) -> bool:
    """Docker Compose profile が有効かを返す。"""
    profiles = re.split(r"[,\s]+", ctx.os.getenv("COMPOSE_PROFILES", ""))
    return profile in {item for item in profiles if item}


def engine_url(
    ctx: ConfigContext,
    env_name: str,
    default: str = "",
    *,
    profile: str | None = None,
    profile_default: str = "",
) -> str:
    """環境変数と任意の Compose profile fallback から engine URL を 1 つ解決する。"""
    if url := ctx.os.getenv(env_name):
        return url
    if profile and compose_profile_enabled(ctx, profile):
        return profile_default
    return default
