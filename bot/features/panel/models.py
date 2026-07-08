"""control-panel feature の data model。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSnapshot:
    """control panel の描画に必要な公開状態。

    属性:
        process_voice_connection_count: この process が現在接続している VC 数。
        license_lines: パネルに表示する音声/ライセンス案内。空なら省略表示にする。
    """

    process_voice_connection_count: int
    license_lines: tuple[str, ...] = ()
