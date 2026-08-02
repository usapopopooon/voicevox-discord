"""読み上げ Bot の Discord UI feature。

Discord 自体も feature adapter として扱う。この module は Discord UI primitive を
必要とする view、modal、embed を所有する。runtime state は composition root から
``configure`` 経由で提供される。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from typing import Any, cast

import discord
from discord import ui

from features.discord_bot import commands as discord_bot_commands
from features.panel import PanelSnapshot
from features.panel import build_panel_embed as build_panel_embed_from_snapshot

type DiscordButton = ui.Button[Any]
type DiscordTextInput = ui.TextInput[Any]

_runtime: Any | None = None


def configure(runtime: Any) -> None:
    """この Discord UI feature を composition runtime へ束縛する。"""
    global _runtime
    _runtime = runtime


def _ctx() -> Any:
    """設定済み composition runtime を返す。"""
    if _runtime is None:
        raise RuntimeError("discord_bot.ui is not configured")
    return _runtime


# --- 辞書UI ---


DICT_PAGE_SIZE = 10


def dict_items_for_page(
    guild_id: int, page: int
) -> tuple[list[tuple[str, str]], int, int]:
    """UI page 1 件分の辞書 entry を返す。

    引数:
        guild_id: custom 辞書を表示する guild。
        page: 要求された 0 始まりの page index。

    戻り値:
        ``(entries, normalized_page, total_pages)`` の tuple。page index は
        clamp されるため、呼び出し側は安全に前後 page を要求できる。
    """
    items = sorted(
        _ctx().guild_dicts.get(guild_id, {}).items(), key=lambda item: item[0]
    )
    total_pages = max(1, (len(items) + DICT_PAGE_SIZE - 1) // DICT_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * DICT_PAGE_SIZE
    return items[start : start + DICT_PAGE_SIZE], page, total_pages


def build_dict_message(guild_id: int, page: int = 0) -> tuple[str, discord.ui.View]:
    """辞書一覧 message と操作 control を作る。

    引数:
        guild_id: 辞書を表示する guild。
        page: 要求された 0 始まりの page index。

    戻り値:
        message content と、正規化済み page 用に設定された ``DictView``。
    """
    d = _ctx().guild_dicts.get(guild_id, {})
    entries, page, total_pages = dict_items_for_page(guild_id, page)
    if entries:
        lines = [f"  {word} → {reading}" for word, reading in entries]
        content = (
            f"辞書設定（{len(d)}件登録済み / {page + 1}/{total_pages}ページ）\n"
            + "\n".join(lines)
        )
    else:
        content = "辞書設定（登録なし）"
    return content, DictView(guild_id, page=page)


class DictDeleteSelect(ui.Select["DictView"]):
    """現在の辞書 page に見えている entry を削除する select menu。"""

    def __init__(self, guild_id: int, page: int):
        """現在 page 用の削除 menu を作る。

        引数:
            guild_id: 辞書 entry を削除する guild。
            page: 現在の辞書 page。
        """
        entries, _, _ = dict_items_for_page(guild_id, page)
        options = [
            discord.SelectOption(
                label=word[:100],
                value=word,
                description=reading[:100],
            )
            for word, reading in entries[:25]
        ]
        super().__init__(
            placeholder="削除する単語を選択",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )
        self.guild_id = guild_id
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        """選択された単語を削除し、辞書 message を更新する。"""
        word = self.values[0]
        await _ctx().delete_dict_entry(self.guild_id, word)
        content, view = build_dict_message(self.guild_id, self.page)
        await interaction.response.edit_message(content=content, view=view)


class DictView(ui.View):
    """guild 辞書 UI の button / select control。"""

    def __init__(self, guild_id: int, *, page: int = 0):
        """辞書 entry の追加・削除・paging 用 control を作る。

        引数:
            guild_id: 辞書を編集する guild。
            page: 現在の 0 始まり page index。
        """
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.page = page
        entries, page, total_pages = dict_items_for_page(guild_id, page)
        self.page = page
        if entries:
            self.add_item(DictDeleteSelect(guild_id, page))
        for child in self.children:
            if isinstance(child, ui.Button):
                if child.custom_id == "dict:prev":
                    child.disabled = page <= 0
                elif child.custom_id == "dict:next":
                    child.disabled = page >= total_pages - 1

    @ui.button(label="追加", style=discord.ButtonStyle.primary, row=0)
    async def add_button(self, interaction: discord.Interaction, button: DiscordButton):
        """辞書 entry 追加用 modal を開く。"""
        await interaction.response.send_modal(DictAddModal(self.guild_id))

    @ui.button(label="削除", style=discord.ButtonStyle.danger, row=0)
    async def delete_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """辞書 entry 手動削除用 modal を開く。"""
        await interaction.response.send_modal(DictDeleteModal(self.guild_id))

    @ui.button(
        label="前へ",
        style=discord.ButtonStyle.secondary,
        custom_id="dict:prev",
        row=2,
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """前の辞書 page へ移動する。"""
        content, view = build_dict_message(self.guild_id, self.page - 1)
        await interaction.response.edit_message(content=content, view=view)

    @ui.button(
        label="次へ",
        style=discord.ButtonStyle.secondary,
        custom_id="dict:next",
        row=2,
    )
    async def next_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """次の辞書 page へ移動する。"""
        content, view = build_dict_message(self.guild_id, self.page + 1)
        await interaction.response.edit_message(content=content, view=view)


class DictAddModal(ui.Modal, title="辞書に追加"):
    """置換元単語と読みの辞書 entry を追加する modal。"""

    word: DiscordTextInput = ui.TextInput(
        label="置換元", placeholder="例: w", max_length=100
    )
    reading: DiscordTextInput = ui.TextInput(
        label="読み", placeholder="例: ダブリュー", max_length=200
    )

    def __init__(self, guild_id: int):
        """guild 1 件に紐づく追加 modal を作る。

        引数:
            guild_id: 新しい entry を追加する辞書の guild。
        """
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        """入力を検証し、entry を保存して辞書 UI を更新する。"""
        word = self.word.value.strip()
        reading = self.reading.value.strip()
        if not word or not reading:
            await interaction.response.send_message(
                "置換元と読みの両方を入力してください", ephemeral=True
            )
            return

        added = await _ctx().add_dict_entry(self.guild_id, word, reading)
        if not added:
            await interaction.response.send_message(
                f"「{word} → {reading}」はビルドイン辞書と完全一致するため "
                "登録不要です（読みを変えれば登録可能）",
                ephemeral=True,
            )
            return

        content, view = build_dict_message(self.guild_id)
        await interaction.response.edit_message(content=content, view=view)


class DictDeleteModal(ui.Modal, title="辞書から削除"):
    """単語指定で辞書 entry を削除する modal。"""

    word: DiscordTextInput = ui.TextInput(
        label="削除する単語", placeholder="例: w", max_length=100
    )

    def __init__(self, guild_id: int):
        """guild 1 件に紐づく削除 modal を作る。

        引数:
            guild_id: entry を削除する辞書の guild。
        """
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        """入力を検証し、単語を削除して辞書 UI を更新する。"""
        word = self.word.value.strip()
        d = _ctx().guild_dicts.get(self.guild_id, {})
        if word not in d:
            await interaction.response.send_message(
                f"「{word}」は辞書に登録されていません", ephemeral=True
            )
            return

        await _ctx().delete_dict_entry(self.guild_id, word)

        content, view = build_dict_message(self.guild_id)
        await interaction.response.edit_message(content=content, view=view)


# --- 操作パネル / 設定 UI ---


def _active_voice_connection_count() -> int:
    """この Bot process が現在接続している VC 数を返す。"""
    count = 0
    for raw_vc in getattr(_ctx().client, "voice_clients", []):
        vc = _ctx()._as_voice_client(raw_vc)
        if vc is not None and _ctx()._is_vc_connected(vc):
            count += 1
    return count


def build_panel_embed(
    _guild: discord.Guild, *, notice: str | None = None
) -> discord.Embed:
    """runtime state を集めて control-panel embed を描画する。

    引数:
        _guild: 既存呼び出し互換のため受け取る Discord guild。
        notice: 接続完了など、その投稿だけに先頭表示する短い案内。

    戻り値:
        panel feature が生成した Discord embed。
    """
    return build_panel_embed_from_snapshot(
        PanelSnapshot(
            process_voice_connection_count=_active_voice_connection_count(),
            license_lines=_ctx()._panel_license_lines(),
        ),
        notice=notice,
    )


def build_voice_settings_embed(guild_id: int, user_id: int) -> discord.Embed:
    """ユーザー 1 人分の private voice-settings embed を作る。

    引数:
        guild_id: ユーザー設定を含む guild。
        user_id: 設定を描画する Discord user。

    戻り値:
        話者と音声パラメータを要約した Discord embed。
    """
    settings = _ctx().get_user_settings(guild_id, user_id)
    embed = discord.Embed(
        title="音声設定",
        description="\n".join(_ctx()._voice_settings_lines(settings)),
        color=0x8B5CF6,
    )
    embed.set_footer(text="変更後は「試聴」で現在のVCにテスト音声を流せます")
    return embed


class VoiceSettingsModal(ui.Modal, title="音声パラメータを編集"):
    """ユーザー単位の音声合成パラメータを編集する modal。"""

    speed: DiscordTextInput = ui.TextInput(
        label="話速 0.5-2.0", default="1.0", max_length=4
    )
    pitch: DiscordTextInput = ui.TextInput(
        label="音高 -0.15-0.15", default="0.0", max_length=6
    )
    intonation: DiscordTextInput = ui.TextInput(
        label="抑揚 0.0-2.0", default="1.0", max_length=4
    )
    volume: DiscordTextInput = ui.TextInput(
        label="音量 0.0-2.0", default="1.0", max_length=4
    )

    def __init__(self, guild_id: int, user_id: int):
        """ユーザーの現在設定で modal を事前入力する。

        引数:
            guild_id: ユーザーの音声設定を含む guild。
            user_id: 設定を編集中の user。
        """
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        current = _ctx().get_user_settings(guild_id, user_id)
        self.speed.default = str(current.speed)
        self.pitch.default = str(current.pitch)
        self.intonation.default = str(current.intonation)
        self.volume.default = str(current.volume)

    def _parse_float(
        self, raw: str, label: str, low: float, high: float
    ) -> tuple[float | None, str | None]:
        """数値 modal field を parse して clamp する。

        引数:
            raw: Discord から受け取った raw text input。
            label: validation error に使う日本語 field label。
            low: 受け入れる最小値。
            high: 受け入れる最大値。

        戻り値:
            成功時は ``(value, None)``。入力が数値でない場合は
            ``(None, error_message)`` を返す。
        """
        try:
            value = float(raw.strip())
        except ValueError:
            return None, f"{label} は数値で入力してください。"
        return max(low, min(high, value)), None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """検証済み音声設定を保存し、settings view を再描画する。"""
        values: list[tuple[str, str, float, float]] = [
            (self.speed.value, "話速", 0.5, 2.0),
            (self.pitch.value, "音高", -0.15, 0.15),
            (self.intonation.value, "抑揚", 0.0, 2.0),
            (self.volume.value, "音量", 0.0, 2.0),
        ]
        parsed: list[float] = []
        for raw, label, low, high in values:
            value, error = self._parse_float(raw, label, low, high)
            if error is not None or value is None:
                await interaction.response.send_message(error, ephemeral=True)
                return
            parsed.append(value)

        current = _ctx().get_user_settings(self.guild_id, self.user_id)
        new_settings = _ctx().VoiceSettings(
            speaker_id=current.speaker_id,
            speed=parsed[0],
            pitch=parsed[1],
            intonation=parsed[2],
            volume=parsed[3],
        )
        _ctx().user_settings[(self.guild_id, self.user_id)] = new_settings
        await _ctx().save_user_setting(self.guild_id, self.user_id, new_settings)
        await interaction.response.send_message(
            embed=build_voice_settings_embed(self.guild_id, self.user_id),
            view=VoiceSettingsView(self.guild_id, self.user_id),
            ephemeral=True,
        )


class VoiceSettingsView(ui.View):
    """音声設定用の private control。

    この view では、control を公開表示せずに、ユーザーがパラメータ編集、
    reset、音声 preview、license 案内確認を行える。
    """

    def __init__(self, guild_id: int, user_id: int):
        """ユーザー 1 人分の voice-settings control view を作る。

        引数:
            guild_id: ユーザー設定を含む guild。
            user_id: private settings view を所有する user。
        """
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id

    @ui.button(label="詳細設定", style=discord.ButtonStyle.primary, row=0)
    async def edit_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """詳細な voice-parameter modal を開く。"""
        await interaction.response.send_modal(
            VoiceSettingsModal(self.guild_id, self.user_id)
        )

    @ui.button(label="標準に戻す", style=discord.ButtonStyle.secondary, row=0)
    async def reset_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """選択中の話者を維持したまま音声パラメータを reset する。"""
        current = _ctx().get_user_settings(self.guild_id, self.user_id)
        new_settings = _ctx().VoiceSettings(speaker_id=current.speaker_id)
        _ctx().user_settings[(self.guild_id, self.user_id)] = new_settings
        await _ctx().save_user_setting(self.guild_id, self.user_id, new_settings)
        await interaction.response.edit_message(
            embed=build_voice_settings_embed(self.guild_id, self.user_id),
            view=VoiceSettingsView(self.guild_id, self.user_id),
        )

    @ui.button(label="試聴", style=discord.ButtonStyle.success, row=0)
    async def test_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """現在設定を使って短い sample を再生する。"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await play_voice_sample(interaction, "音声設定のテストです")

    @ui.button(label="ライセンス", style=discord.ButtonStyle.secondary, row=1)
    async def license_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """現在の話者設定に対応する license 案内を表示する。"""
        await _ctx()._respond(
            interaction,
            embed=_ctx()._build_license_embed(
                guild_id=self.guild_id, user_id=self.user_id
            ),
            ephemeral=True,
        )


def characters_for_engine(engine_name: str) -> list[str]:
    """engine に属するキャラクター表示名を返す。

    引数:
        engine_name: ユーザーが選択した設定済み engine 名。

    戻り値:
        sort 済みキャラクター名。single-engine mode では engine prefix による
        識別が不要なため、すべてのキャラクターを返す。
    """
    if not _ctx().characters:
        return []
    if len(_ctx().ENGINES) == 1:
        return sorted(_ctx().characters)
    prefix = f"[{engine_name}] "
    return sorted(name for name in _ctx().characters if name.startswith(prefix))


SPEAKER_PAGE_SIZE = 25


def paginate_items[Item](
    items: Sequence[Item], page: int, *, page_size: int = SPEAKER_PAGE_SIZE
) -> tuple[list[Item], int, int]:
    """clamp 済み page index とともに item 1 page 分を返す。

    引数:
        items: paging 対象の sequence。
        page: 要求された 0 始まりの page index。
        page_size: 1 page あたりの最大 item 数。

    戻り値:
        ``(page_items, normalized_page, total_pages)`` を返す。
    """
    # Discord Select は最大 25 options。ここを共通化しておくと、
    # キャラクター/スタイル双方で同じ境界条件（空・最終ページ超過）を扱える。
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return list(items[start : start + page_size]), page, total_pages


class SpeakerEngineSelect(ui.Select["SpeakerPickerView"]):
    """TTS engine を選ぶ 1 step 目の select menu。"""

    def __init__(self):
        """設定済み engine から option を作る。"""
        # SelectOption の上限に合わせて 25 件まで。通常は 3 engine 程度だが、
        # 将来増やしても Discord API の制約違反で落ちないようにする。
        options = [
            discord.SelectOption(label=name, value=name)
            for name, _, _ in _ctx().ENGINES[:25]
        ]
        super().__init__(
            placeholder="音声エンジンを選択",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """engine 選択からキャラクター選択へ進む。"""
        engine_name = self.values[0]
        parent = cast(Any, self.view)
        _, page, total_pages = paginate_items(characters_for_engine(engine_name), 0)
        await interaction.response.edit_message(
            embed=build_speaker_picker_embed(
                engine_name,
                page=page,
                total_pages=total_pages,
                page_label="キャラクター",
            ),
            view=SpeakerCharacterView(
                parent.guild_id, parent.user_id, engine_name, page
            ),
        )


class SpeakerCharacterSelect(ui.Select["SpeakerCharacterView"]):
    """話者キャラクターを選ぶ 2 step 目の select menu。"""

    def __init__(self, engine_name: str, page: int):
        """engine と page に対応するキャラクター option を作る。

        引数:
            engine_name: キャラクター一覧を表示する engine。
            page: 現在のキャラクター page。
        """
        char_names = characters_for_engine(engine_name)
        visible_items, _, _ = paginate_items(char_names, page)
        options = [
            discord.SelectOption(label=char_name[:100], value=char_name)
            for char_name in visible_items
        ] or [
            discord.SelectOption(
                label="候補がありません",
                value="__none__",
                description="スピーカー一覧を再取得してください",
            )
        ]
        super().__init__(
            placeholder="キャラクターを選択",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.disabled = options[0].value == "__none__"

    async def callback(self, interaction: discord.Interaction) -> None:
        """キャラクター選択からスタイル選択へ進む。"""
        char_name = self.values[0]
        parent = cast(Any, self.view)
        _, page, total_pages = paginate_items(_ctx().characters.get(char_name, []), 0)
        await interaction.response.edit_message(
            embed=build_speaker_picker_embed(
                parent.engine_name,
                char_name,
                page=page,
                total_pages=total_pages,
                page_label="スタイル",
            ),
            view=SpeakerStyleView(
                parent.guild_id,
                parent.user_id,
                parent.engine_name,
                char_name,
                page,
            ),
        )


class SpeakerStyleSelect(ui.Select["SpeakerStyleView"]):
    """話者スタイルを選ぶ最終 step の select menu。"""

    def __init__(self, char_name: str, page: int):
        """キャラクターと page に対応するスタイル option を作る。

        引数:
            char_name: 選択済みキャラクター表示名。
            page: 現在のスタイル page。
        """
        styles = _ctx().characters.get(char_name, [])
        visible_items, _, _ = paginate_items(styles, page)
        options = [
            discord.SelectOption(label=style_name[:100], value=str(global_id))
            for global_id, style_name in visible_items
        ] or [
            discord.SelectOption(
                label="候補がありません",
                value="__none__",
                description="キャラクターを選び直してください",
            )
        ]
        super().__init__(
            placeholder="スタイルを選択",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.disabled = options[0].value == "__none__"

    async def callback(self, interaction: discord.Interaction) -> None:
        """選択された話者スタイルを保存し、音声設定へ戻る。"""
        speaker_id = int(self.values[0])
        parent = cast(Any, self.view)
        current = _ctx().get_user_settings(parent.guild_id, parent.user_id)
        new_settings = _ctx().VoiceSettings(
            speaker_id=speaker_id,
            speed=current.speed,
            pitch=current.pitch,
            intonation=current.intonation,
            volume=current.volume,
        )
        _ctx().user_settings[(parent.guild_id, parent.user_id)] = new_settings
        await _ctx().save_user_setting(parent.guild_id, parent.user_id, new_settings)
        await interaction.response.edit_message(
            embed=build_voice_settings_embed(parent.guild_id, parent.user_id),
            view=VoiceSettingsView(parent.guild_id, parent.user_id),
        )


def build_speaker_picker_embed(
    engine_name: str | None = None,
    char_name: str | None = None,
    *,
    page: int | None = None,
    total_pages: int | None = None,
    page_label: str = "候補",
) -> discord.Embed:
    """multi-step speaker picker 用の案内文を作る。

    引数:
        engine_name: 現在選択されている engine。未選択なら ``None``。
        char_name: 現在選択されているキャラクター。未選択なら ``None``。
        page: 現在の選択 step の page index。
        total_pages: 現在の選択 step の総 page 数。
        page_label: paging 対象 item 種別の日本語 label。

    戻り値:
        現在の話者選択 step を説明する Discord embed。
    """
    lines = ["エンジン、キャラクター、スタイルの順に選択してください。"]
    if engine_name:
        lines.append(f"選択中のエンジン: {engine_name}")
    if char_name:
        lines.append(f"選択中のキャラクター: {char_name}")
    if page is not None and total_pages is not None and total_pages > 1:
        lines.append(f"{page_label}: {page + 1}/{total_pages}ページ")
    if engine_name and len(characters_for_engine(engine_name)) > 25:
        lines.append("候補が多いため、25件ずつ表示しています。")
    return discord.Embed(
        title="読み上げキャラクター変更",
        description="\n".join(lines),
        color=0x06B6D4,
    )


class SpeakerPickerView(ui.View):
    """speaker picker flow の root view。"""

    def __init__(self, guild_id: int, user_id: int):
        """1 step 目の engine picker を作る。

        引数:
            guild_id: ユーザー設定を含む guild。
            user_id: 話者設定を変更する user。
        """
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(SpeakerEngineSelect())


class SpeakerCharacterView(ui.View):
    """engine 選択後にキャラクターを選ぶ view。"""

    def __init__(self, guild_id: int, user_id: int, engine_name: str, page: int = 0):
        """engine 1 件分の paged character picker を作る。

        引数:
            guild_id: ユーザー設定を含む guild。
            user_id: 話者設定を変更する user。
            engine_name: 選択済み engine。
            page: 要求されたキャラクター page。
        """
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.engine_name = engine_name
        _, page, total_pages = paginate_items(characters_for_engine(engine_name), page)
        self.page = page
        self.total_pages = total_pages
        self.add_item(SpeakerCharacterSelect(engine_name, page))
        if total_pages > 1:
            self.add_item(SpeakerCharacterPageButton(-1, disabled=page <= 0))
            self.add_item(
                SpeakerCharacterPageButton(1, disabled=page >= total_pages - 1)
            )


class SpeakerStyleView(ui.View):
    """キャラクター選択後にスタイルを選ぶ view。"""

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        engine_name: str,
        char_name: str,
        page: int = 0,
    ):
        """キャラクター 1 件分の paged style picker を作る。

        引数:
            guild_id: ユーザー設定を含む guild。
            user_id: 話者設定を変更する user。
            engine_name: 選択済み engine。
            char_name: 選択済みキャラクター。
            page: 要求されたスタイル page。
        """
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.user_id = user_id
        self.engine_name = engine_name
        self.char_name = char_name
        _, page, total_pages = paginate_items(
            _ctx().characters.get(char_name, []), page
        )
        self.page = page
        self.total_pages = total_pages
        self.add_item(SpeakerStyleSelect(char_name, page))
        if total_pages > 1:
            self.add_item(SpeakerStylePageButton(-1, disabled=page <= 0))
            self.add_item(SpeakerStylePageButton(1, disabled=page >= total_pages - 1))


class SpeakerCharacterPageButton(ui.Button["SpeakerCharacterView"]):
    """キャラクター選択用の pagination button。"""

    def __init__(self, direction: int, *, disabled: bool):
        """前後キャラクター page button を作る。

        引数:
            direction: 前へなら ``-1``、次へなら ``1``。
            disabled: 端の page で button を disabled にするか。
        """
        super().__init__(
            label="前へ" if direction < 0 else "次へ",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        """隣接 page で character picker を更新する。"""
        parent = cast(Any, self.view)
        next_page = parent.page + self.direction
        _, page, total_pages = paginate_items(
            characters_for_engine(parent.engine_name), next_page
        )
        await interaction.response.edit_message(
            embed=build_speaker_picker_embed(
                parent.engine_name,
                page=page,
                total_pages=total_pages,
                page_label="キャラクター",
            ),
            view=SpeakerCharacterView(
                parent.guild_id, parent.user_id, parent.engine_name, page
            ),
        )


class SpeakerStylePageButton(ui.Button["SpeakerStyleView"]):
    """スタイル選択用の pagination button。"""

    def __init__(self, direction: int, *, disabled: bool):
        """前後スタイル page button を作る。

        引数:
            direction: 前へなら ``-1``、次へなら ``1``。
            disabled: 端の page で button を disabled にするか。
        """
        super().__init__(
            label="前へ" if direction < 0 else "次へ",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        """隣接 page で style picker を更新する。"""
        parent = cast(Any, self.view)
        next_page = parent.page + self.direction
        _, page, total_pages = paginate_items(
            _ctx().characters.get(parent.char_name, []), next_page
        )
        await interaction.response.edit_message(
            embed=build_speaker_picker_embed(
                parent.engine_name,
                parent.char_name,
                page=page,
                total_pages=total_pages,
                page_label="スタイル",
            ),
            view=SpeakerStyleView(
                parent.guild_id,
                parent.user_id,
                parent.engine_name,
                parent.char_name,
                page,
            ),
        )


async def refresh_panel_message(
    interaction: discord.Interaction, guild: discord.Guild
) -> bool:
    """操作後、元のパネルメッセージを最新状態へ更新できたか返す。"""
    message = getattr(interaction, "message", None)
    edit = getattr(message, "edit", None)
    if not callable(edit):
        return False
    try:
        result = edit(embed=build_panel_embed(guild), view=ControlPanelView(guild))
        if inspect.isawaitable(result):
            await result
        return True
    except Exception as e:
        _ctx().logger.debug(f"操作パネルの更新に失敗: {e}")
        return False


class ControlPanelView(ui.View):
    """読み上げ Bot のよく使う操作をまとめた公開 control panel。"""

    def __init__(self, guild: discord.Guild | None = None):
        """panel button を作り、guild があれば現在不可能な操作を disabled にする。

        引数:
            guild: button availability を決める voice state を持つ guild。
                永続 view 登録時は未指定にし、callback 側で guild を解決する。
        """
        super().__init__(timeout=None)
        self.guild_id = guild.id if guild is not None else None
        if guild is None:
            return
        vc = _ctx()._as_voice_client(guild.voice_client)
        connected = vc is not None and _ctx()._is_vc_connected(vc)
        playing = vc is not None and _ctx()._is_vc_playing(vc)
        for child in self.children:
            if not isinstance(child, ui.Button):
                continue
            if child.custom_id == "panel:connect":
                child.disabled = connected
            elif child.custom_id == "panel:disconnect":
                child.disabled = not connected
            elif child.custom_id == "panel:skip":
                child.disabled = not playing

    @ui.button(
        label="接続",
        style=discord.ButtonStyle.primary,
        custom_id="panel:connect",
        row=0,
    )
    async def connect_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """呼び出し者の voice channel へ Bot を接続し、panel を更新する。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        if _ctx()._has_active_voice_connection(guild):
            await _ctx()._respond(
                interaction,
                content="すでにVCに接続しています。",
                ephemeral=True,
            )
            await refresh_panel_message(interaction, guild)
            return
        await discord_bot_commands.join(_ctx(), interaction, panel_response="update")

    @ui.button(
        label="切断",
        style=discord.ButtonStyle.danger,
        custom_id="panel:disconnect",
        row=0,
    )
    async def disconnect_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """Bot を VC から切断し、古い runtime voice state を消す。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        if not _ctx()._has_active_voice_connection(guild):
            await _ctx()._reset_voice_state(guild)
            await _ctx()._respond(
                interaction,
                content="ボイスチャンネルに接続していません。",
                ephemeral=True,
            )
            await refresh_panel_message(interaction, guild)
            return
        await discord_bot_commands.leave(_ctx(), interaction, panel_response="update")

    @ui.button(
        label="スキップ",
        style=discord.ButtonStyle.danger,
        custom_id="panel:skip",
        row=0,
    )
    async def skip_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """再生中の場合、現在再生している audio を skip する。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        vc = _ctx()._as_voice_client(guild.voice_client)
        if vc is None or not _ctx()._is_vc_playing(vc):
            await _ctx()._respond(
                interaction,
                content="再生中の音声はありません。",
                ephemeral=True,
            )
            await refresh_panel_message(interaction, guild)
            return
        await discord_bot_commands.skip(_ctx(), interaction, panel_response="update")

    @ui.button(
        label="話者変更",
        style=discord.ButtonStyle.primary,
        custom_id="panel:speaker",
        row=1,
    )
    async def speaker_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """private な multi-step speaker picker を開く。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        if (
            not _ctx().characters
            or not _ctx().speaker_engine
            or _ctx()._has_missing_configured_speaker_engines()
        ):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _ctx()._refresh_speakers_if_needed()
        await _ctx()._respond(
            interaction,
            embed=build_speaker_picker_embed(),
            view=SpeakerPickerView(guild.id, interaction.user.id),
            ephemeral=True,
        )

    @ui.button(
        label="音声設定",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:voice",
        row=1,
    )
    async def voice_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """private な voice parameter control を開く。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        await _ctx()._respond(
            interaction,
            embed=build_voice_settings_embed(guild.id, interaction.user.id),
            view=VoiceSettingsView(guild.id, interaction.user.id),
            ephemeral=True,
        )

    @ui.button(
        label="辞書",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:dict",
        row=1,
    )
    async def dict_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """private な guild 辞書 control を開く。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        content, view = build_dict_message(guild.id)
        await _ctx()._respond(interaction, content=content, view=view, ephemeral=True)

    @ui.button(
        label="状態",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:status",
        row=2,
    )
    async def status_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """公開してよい status embed を private に表示する。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        await _ctx()._respond(
            interaction,
            embed=_ctx()._build_status_embed(guild),
            ephemeral=True,
        )

    @ui.button(
        label="ライセンス",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:license",
        row=2,
    )
    async def panel_license_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """現在ユーザーの話者に対応する license / credit 案内を表示する。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        await _ctx()._respond(
            interaction,
            embed=_ctx()._build_license_embed(
                guild_id=guild.id, user_id=interaction.user.id
            ),
            ephemeral=True,
        )

    @ui.button(
        label="新規投稿",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:repost",
        row=2,
    )
    async def repost_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """最新内容で新しい公開 panel message を投稿する。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        await interaction.response.send_message(
            embed=build_panel_embed(guild),
            view=ControlPanelView(guild),
        )

    @ui.button(
        label="更新",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:refresh",
        row=2,
    )
    async def refresh_button(
        self, interaction: discord.Interaction, button: DiscordButton
    ):
        """panel embed と button の enabled/disabled 状態を更新する。"""
        guild = await _ctx()._require_guild_interaction(interaction)
        if guild is None:
            return
        await interaction.response.edit_message(
            embed=build_panel_embed(guild),
            view=ControlPanelView(guild),
        )


async def play_voice_sample(interaction: discord.Interaction, text: str) -> None:
    """現在の VC で短い sample を合成して再生する。

    引数:
        interaction: sample を要求した interaction。
        text: リクエスト者の現在設定で合成する text。
    """
    guild = await _ctx()._require_guild_interaction(interaction)
    if guild is None:
        return
    vc = _ctx()._as_voice_client(guild.voice_client)
    if vc is None or not _ctx()._is_vc_connected(vc):
        await _ctx()._respond(
            interaction,
            content="先にBotをVCへ接続してください。",
            ephemeral=True,
        )
        return
    trace_id = _ctx()._new_trace_id()
    try:
        settings = _ctx().get_user_settings(guild.id, interaction.user.id)
        audio_data = await _ctx().synthesize(text, settings, cache=True)
        _ctx()._ensure_queue(guild.id).append(audio_data)
        if _ctx()._can_start_playback(vc):
            await _ctx().play_next(guild.id, vc)
        _ctx()._log_event(
            logging.INFO,
            "voice.sample.played",
            trace_id=trace_id,
            guild_id=guild.id,
            user_id=interaction.user.id,
        )
        await _ctx()._respond(
            interaction, content="試聴音声を再生しました。", ephemeral=True
        )
    except Exception as e:
        _ctx()._record_recent_error(
            "voice.sample.failed", str(e), trace_id, guild_id=guild.id
        )
        _ctx()._log_event(
            logging.WARNING,
            "voice.sample.failed",
            trace_id=trace_id,
            guild_id=guild.id,
            user_id=interaction.user.id,
            error=str(e),
        )
        await _ctx()._respond(
            interaction,
            content="試聴に失敗しました。しばらくしてから再試行してください。",
            ephemeral=True,
        )


# test と composition root 向けの後方互換 alias。新規 code は上の公開名を使う。
# 公開名は TypeScript export へも移しやすい形にしている。
# bot.py 側の古い `_build_*` import を壊さないための橋。新規利用は公開名を使う。
_dict_items_for_page = dict_items_for_page
_build_panel_embed = build_panel_embed
_build_voice_settings_embed = build_voice_settings_embed
_characters_for_engine = characters_for_engine
_page_items = paginate_items
_build_speaker_picker_embed = build_speaker_picker_embed
_refresh_panel_message = refresh_panel_message
_play_voice_sample = play_voice_sample
