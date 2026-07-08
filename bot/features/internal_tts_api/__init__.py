"""音声合成用の internal HTTP API feature。"""

from .aiohttp_adapter import (
    authorized,
    health,
    payload_float,
    payload_int,
    prepare_text,
    should_start,
    start,
    stop,
    synthesize,
)

__all__ = [
    "authorized",
    "health",
    "payload_float",
    "payload_int",
    "prepare_text",
    "should_start",
    "start",
    "stop",
    "synthesize",
]
