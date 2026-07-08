"""control-panel embed の presentation test。"""

from features.panel import PanelSnapshot, build_panel_embed


def test_panel_embed_shows_process_connection_count_when_disconnected():
    """未接続 panel はプロセス単位の VC 接続数だけを表示すること。"""
    embed = build_panel_embed(
        PanelSnapshot(
            process_voice_connection_count=0,
        )
    )

    values = "\n".join(field.value for field in embed.fields)
    field_names = "\n".join(field.name for field in embed.fields)

    assert embed.description is None
    assert "接続数" in field_names
    assert "0 VC" in values
    assert "現在の状態" not in field_names
    assert "VC:" not in values
    assert "読み上げ対象" not in values
    assert "キュー:" not in values
    assert "再生:" not in values


def test_panel_embed_shows_process_connection_count_when_connected():
    """接続中 panel はプロセス単位の VC 接続数を表示すること。"""
    embed = build_panel_embed(
        PanelSnapshot(
            process_voice_connection_count=3,
        )
    )

    values = "\n".join(field.value for field in embed.fields)
    field_names = "\n".join(field.name for field in embed.fields)

    assert embed.description is None
    assert "接続数" in field_names
    assert "3 VC" in values
    assert "現在の状態" not in field_names


def test_panel_embed_keeps_only_connection_count_and_commands():
    """panel 本体は接続数とコマンドに絞り、ボタン一覧の説明を重複表示しないこと。"""
    embed = build_panel_embed(
        PanelSnapshot(
            process_voice_connection_count=0,
        ),
        notice="「General」に接続しました",
    )

    values = "\n".join(field.value for field in embed.fields)
    field_names = "\n".join(field.name for field in embed.fields)

    assert "「General」に接続しました" in (embed.description or "")
    assert "パネルでできること" not in field_names
    assert "接続 / 切断 / スキップ" not in values
    assert "新しいパネル投稿" not in values
    assert "コマンド" in field_names
    assert "補助操作" not in field_names
    assert "残しているコマンド" not in field_names
    assert "`/vc`" in values
    assert "`/panel`" in values
    assert "`/mute`" in values
    assert "`/join`" not in values
