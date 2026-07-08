"""control-panel feature の data model。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSnapshot:
    """control panel の描画に必要な公開状態。

    属性:
        connected: Bot が現在有効な voice connection を持っているか。
        playing: 有効な voice client が音声を再生中か。
        voice_channel_name: 現在の voice channel として表示する名前。
        read_channel_id: Bot が読む text channel。未設定なら ``None``。
        queue_length: 現在 queue に入っている音声 item 数。
        queue_maxlen: 設定済み queue 容量。
        license_lines: パネルに表示する音声/ライセンス案内。空なら省略表示にする。
    """

    connected: bool
    playing: bool
    voice_channel_name: str
    read_channel_id: int | None
    queue_length: int
    queue_maxlen: int
    license_lines: tuple[str, ...] = ()
