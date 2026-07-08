"""internal TTS API の payload validation test。"""

import pytest

from .aiohttp_adapter import payload_float, payload_int


def test_payload_number_rejects_bool() -> None:
    """JSON boolean を Python integer として黙って通さないこと。"""
    with pytest.raises(ValueError, match="speed must be a number"):
        payload_float({"speed": True}, "speed", 1.0, 0.5, 2.0)

    with pytest.raises(ValueError, match="speaker_id must be a number"):
        payload_int({"speaker_id": False}, "speaker_id", 46, 0, 99999)


def test_payload_number_accepts_numeric_strings() -> None:
    """緩い JSON client 向けに文字列の数値は引き続き受け入れること。"""
    assert payload_float({"speed": "1.25"}, "speed", 1.0, 0.5, 2.0) == 1.25
    assert payload_int({"speaker_id": "46"}, "speaker_id", 0, 0, 99999) == 46
