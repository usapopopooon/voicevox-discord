"""status feature の data model。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusSnapshot:
    """ユーザー向け status embed に表示してよい公開状態。

    属性:
        connected: Bot が現在有効な voice connection を持っているか。
        voice_channel_name: 現在の voice channel として表示する名前。
        read_channel_id: Bot が読む text channel。未設定なら ``None``。
        queue_length: 現在 queue に入っている音声 item 数。
        queue_maxlen: 設定済み queue 容量。
        speaker_count: 読み込み済み話者候補数。
        configured_engines: この Bot instance に設定されている engine 名。
        healthy_engines: 話者 metadata を返した engine 名。
    """

    connected: bool
    voice_channel_name: str
    read_channel_id: int | None
    queue_length: int
    queue_maxlen: int
    speaker_count: int
    configured_engines: tuple[str, ...]
    healthy_engines: tuple[str, ...]
