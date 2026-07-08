"""text-processing runtime-state bridge の test。"""

from dataclasses import replace

from features.dictionary import text_processing


def test_runtime_state_bridge_supports_public_rebuild() -> None:
    """明示的な state DTO で古い stringly module mutation を置き換えられること。"""
    original_state = text_processing.export_runtime_state()
    try:
        text_processing.import_runtime_state(
            replace(
                original_state,
                kaomoji_dict={"(移)": "いしょく"},
            )
        )
        text_processing.rebuild_kaomoji_patterns()

        state = text_processing.export_runtime_state()
        assert state.kaomoji_pattern is not None
        assert state.kaomoji_pattern.search("(移)") is not None
        assert text_processing.clean_text("あ(移)い") == "あいしょくい"
    finally:
        text_processing.import_runtime_state(original_state)
        text_processing.rebuild_kaomoji_patterns()
