"""control-panel embed の presentation test。"""

from features.panel import PanelSnapshot, build_panel_embed


def test_panel_embed_formats_disconnected_state():
    """未接続 panel は有効な VC がないことを明確に示すこと。"""
    embed = build_panel_embed(
        PanelSnapshot(
            connected=False,
            playing=False,
            voice_channel_name="未接続",
            read_channel_id=None,
            queue_length=0,
            queue_maxlen=100,
        )
    )

    values = "\n".join(field.value for field in embed.fields)

    assert "VC: 未接続" in values
    assert "接続: 未接続" in values
    assert "再生: 未接続" in values


def test_panel_embed_formats_playing_state():
    """有効な panel は VC、読み上げ channel、接続、再生状態を表示すること。"""
    embed = build_panel_embed(
        PanelSnapshot(
            connected=True,
            playing=True,
            voice_channel_name="General",
            read_channel_id=123,
            queue_length=2,
            queue_maxlen=100,
        )
    )

    values = "\n".join(field.value for field in embed.fields)

    assert "VC: General" in values
    assert "<#123>" in values
    assert "接続: 接続中" in values
    assert "再生: 再生中" in values
