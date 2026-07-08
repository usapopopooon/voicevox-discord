"""license feature の公開 API。

import 側は ``domain`` や ``presentation`` へ直接入らず、この module を使う。
後で application layer や infrastructure layer が増えても call site を安定させるため。
"""

from .domain import (
    credit_for_speaker,
    engine_name_from_speaker_display_name,
    normalize_speaker_display_name,
)
from .models import CurrentCredit, LicenseInfo
from .presentation import LICENSE_INFOS, build_license_embed

__all__ = [
    "LICENSE_INFOS",
    "CurrentCredit",
    "LicenseInfo",
    "build_license_embed",
    "credit_for_speaker",
    "engine_name_from_speaker_display_name",
    "normalize_speaker_display_name",
]
