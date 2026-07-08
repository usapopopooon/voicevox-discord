"""Bot 本体に依存しない音声合成 feature。"""

from .application import (
    SynthCacheKey,
    SynthCandidate,
    VoiceSynthesisSettings,
    build_synthesis_candidates,
    has_missing_configured_speaker_engines,
    lookup_recent_synth_cache,
    lookup_synth_cache,
    prune_candidate_fail_until,
    refresh_missing_speakers_if_needed,
    refresh_speakers_if_needed,
    run_candidates,
    schedule_missing_speaker_refresh,
    store_recent_synth_cache,
    store_synth_cache,
    synth_cache_key,
    synthesize,
    try_candidate,
)
from .infrastructure import fetch_speakers, synthesize_with_candidate

__all__ = [
    "SynthCandidate",
    "SynthCacheKey",
    "VoiceSynthesisSettings",
    "build_synthesis_candidates",
    "fetch_speakers",
    "has_missing_configured_speaker_engines",
    "lookup_recent_synth_cache",
    "lookup_synth_cache",
    "prune_candidate_fail_until",
    "refresh_missing_speakers_if_needed",
    "refresh_speakers_if_needed",
    "run_candidates",
    "schedule_missing_speaker_refresh",
    "store_recent_synth_cache",
    "store_synth_cache",
    "synth_cache_key",
    "synthesize",
    "synthesize_with_candidate",
    "try_candidate",
]
