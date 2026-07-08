# VOICEVOX 読み上げ Discord Bot - アーキテクチャ

## 概要

Discord のテキストチャンネルに投稿されたメッセージを、VOICEVOX の音声でボイスチャンネルに読み上げる Bot。
ギルドごとに独立したユーザー音声設定・辞書・ミュートを管理する。

## システム構成

```
┌──────────────────────────────────────────────────┐
│  Coolify / Docker Compose                        │
│                                                  │
│  ┌──────────────┐    ┌───────────────────────┐   │
│  │ voicevox-    │───→│ voicevox              │   │
│  │ discord      │    │                       │   │
│  │ (Python)     │    │ (VOICEVOX Engine CPU)  │   │
│  │              │    │ :50021                 │   │
│  └──────┬───────┘    └───────────────────────┘   │
│         │                                        │
│         │  ┌───────────────────────┐             │
│         └─→│ PostgreSQL            │             │
│            │ (永続化)              │             │
│            └───────────────────────┘             │
└──────────────────────────────────────────────────┘
          │
    Discord API
```

- **voicevox-discord**: Bot 本体。スラッシュコマンドとメッセージ読み上げを処理
- **voicevox**: VOICEVOX Engine (CPU版)。テキスト→音声合成 API
- **PostgreSQL**: ユーザー設定・辞書の永続化

## ディレクトリ構成

```
voicevox-discord/
├── bot/
│   ├── bot.py                ← composition root / 既存互換レイヤ（2000行未満）
│   ├── app/                  ← 起動・設定・DBプール・プロセス監督のみ
│   │   ├── config.py
│   │   ├── database.py
│   │   └── launcher.py
│   ├── features/             ← package by feature の本体
│   │   ├── dictionary/
│   │   │   ├── __init__.py
│   │   │   ├── application.py
│   │   │   ├── builtin_readings.py
│   │   │   ├── builtin_kaomoji/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── actions.py
│   │   │   │   ├── negative.py
│   │   │   │   ├── positive.py
│   │   │   │   ├── reactions.py
│   │   │   │   ├── slang.py
│   │   │   │   └── social.py
│   │   │   ├── infrastructure.py
│   │   │   └── text_processing.py
│   │   ├── discord_bot/
│   │   │   ├── commands.py
│   │   │   ├── help.py
│   │   │   ├── lifecycle.py
│   │   │   ├── messages.py
│   │   │   └── ui.py
│   │   ├── internal_tts_api/
│   │   │   └── aiohttp_adapter.py
│   │   ├── license/
│   │   │   ├── __init__.py
│   │   │   ├── models.py
│   │   │   ├── domain.py
│   │   │   ├── presentation.py
│   │   │   ├── test_domain.py
│   │   │   └── test_presentation.py
│   │   ├── panel/
│   │   │   ├── __init__.py
│   │   │   ├── models.py
│   │   │   ├── presentation.py
│   │   │   └── test_presentation.py
│   │   ├── status/
│   │   │   ├── __init__.py
│   │   │   ├── models.py
│   │   │   ├── presentation.py
│   │   │   └── test_presentation.py
│   │   ├── voice_playback/
│   │   │   ├── application.py
│   │   │   └── discord_adapter.py
│   │   ├── voice_sessions/
│   │   │   ├── discord_adapter.py
│   │   │   └── infrastructure.py
│   │   ├── voice_settings/
│   │   │   └── infrastructure.py
│   │   └── voice_synthesis/
│   │       ├── application.py
│   │       └── infrastructure.py
│   ├── migrations/           ← DB マイグレーションと runner
│   │   ├── runner.py
│   │   └── 2026....py
│   ├── Dockerfile            ← 本番用
│   ├── Dockerfile.dev        ← 開発用 (watchdog ホットリロード)
│   ├── requirements.txt      ← 本番依存
│   ├── requirements.dev.txt  ← 開発追加依存
│   └── tests/
│       ├── conftest.py       ← テスト用環境変数
│       └── test_bot.py       ← 既存互換・統合寄りの回帰テスト
├── voicevox/
│   └── Dockerfile            ← VOICEVOX Engine ラッパー
├── docs/
│   └── ARCHITECTURE.md       ← このファイル
├── docker-compose.yml          ← 共通ベース定義
├── docker-compose.override.yml ← ローカル開発用上書き (自動適用)
├── pyproject.toml              ← ruff / pyright / pytest 設定
├── pyright-pylance-strict.json ← Pylance 寄り strict 型チェック設定
├── .github/workflows/ci.yml   ← GitHub Actions CI
├── .env.example                ← 環境変数テンプレート
├── .gitignore
└── .dockerignore
```

## Package By Feature / Clean Architecture / コロケーションテスト

このプロジェクトでは、[React のフォルダ構成、段階的に考えてみた ― Package by Feature から Clean Architecture まで](https://zenn.dev/yoshi333/articles/b25d7ff4915a57)
の「Package by Feature を土台にし、複雑になった feature だけ層を導入する」考え方を、
Python / Discord Bot 向けに読み替えて採用する。

新規の利用者向け機能は、原則として `bot/features/<feature>/` に配置する。
小さい feature は `models.py` と `presentation.py` 程度に留め、複雑化した feature だけ
`domain.py`, `application.py`, `infrastructure.py` を追加する。最初から全 feature に同じ層を
強制しない。

- `bot.py` は composition root と既存互換 wrapper に限定し、2000行以上にしない。
- Discord Bot であること、音声 Bot であること、内部 HTTP API であることも feature として扱う。
  `discord_bot`, `voice_synthesis`, `voice_playback`, `internal_tts_api` のように名前を付けて分ける。
- `app/` は feature ではない。起動設定、DB接続プール、プロセス監督など、feature を組み立てる
  配線だけを置く。音声処理、Discord UI、SQL、辞書キャッシュを `app/` に置かない。
- SQL は各 feature の `infrastructure.py` に置く。`dictionary` の SQL は `dictionary`、
  `voice_settings` の SQL は `voice_settings`、`mutes` の SQL は `mutes` が所有する。
- キャッシュはその振る舞いを所有する feature に置く。辞書置換キャッシュは `dictionary`、
  合成キャッシュは `voice_synthesis` が所有する。
- feature パッケージは Discord adapter から渡された snapshot を受け取り、利用者に見える
  UI 表現を返す。これにより、feature 単体のテストでは Bot 起動や DB 接続を不要にする。
- `bot/tests/test_bot.py` は移行期間の互換テストと統合寄りの回帰テストとして残す。
- feature に名前を付けられるものは `shared` に逃がさない。読み辞書・顔文字辞書は
  `dictionary` feature の責務とする。
- 複数 feature から本当に再利用され、どの feature にも所有させにくい処理だけを
  `shared` 化する。現時点では `shared/` を作らず、必要になるまで単一 feature 内に閉じ込める。
- テストは実装の近くに置く。例: `domain.py` には `test_domain.py`、
  `presentation.py` には `test_presentation.py` を同じ feature パッケージ内に置く。
- テスト実行は `pytest bot` を標準にし、従来の `bot/tests` だけでなく
  colocated test も CI で必ず収集する。
- 型チェックは通常の `pyright` に加えて、Pylance 寄りの
  `pyright --project pyright-pylance-strict.json` を CI で必ず実行する。

### 読みやすさ / TypeScript 移植しやすさ

Python 実装であっても、将来 TypeScript へ置き換える可能性を前提に、境界は
「名前付きのデータ構造」と「明示的な関数」で表現する。

- feature 境界では `dict[str, Any]` や module の暗黙参照ではなく、
  `dataclass` / `Protocol` / `Mapping` など、TypeScript の `interface` に対応しやすい形を優先する。
- `getattr`, `setattr`, `globals()`, `sys.modules[__name__]` は移行期の互換層に閉じ込める。
  新規 feature へ広げない。
- `bot.py` が一時的に runtime context として渡される場合も、呼び出し側では
  `_runtime_context()` を使う。TypeScript 化ではここを `AppContext` の明示的なインスタンスへ置き換える。
- feature が runtime context を受け取る場合は、可能な範囲で `Protocol` を定義する。
  例: `ConfigContext`, `PlaybackStateContext`, `DiscordPlaybackContext`,
  `InternalTtsApiContext` は TypeScript の `interface` へほぼ機械的に写せる形にしておく。
- module 変数をまたいだ同期が必要な場合は、文字列で直接触らず、`TextProcessingRuntimeState`
  のような名前付き DTO と import/export 関数を用意する。
- 純粋な変換・判定は `domain.py` / `application.py` に寄せ、Discord / aiohttp / asyncpg 依存は
  adapter / infrastructure に閉じ込める。これは TypeScript でも同じ層に移しやすい。

### Feature 内の層

| 層 | 役割 | 依存してよいもの |
|---|---|---|
| `models.py` | feature 内で受け渡すデータ構造。snapshot / DTO / 値オブジェクト | 標準ライブラリのみ |
| `domain.py` | ビジネスルール、正規化、判定、純粋関数 | `models.py` |
| `application.py` | 操作単位のユースケース。複数 domain 処理の組み合わせ | `models.py`, `domain.py` |
| `infrastructure.py` | 外部 I/O。DB、HTTP、ファイル、外部 API との接続 | `models.py`、必要なら protocol |
| `discord_adapter.py` / `aiohttp_adapter.py` | Discord や aiohttp など特定チャネルへの接続 | `application.py`, `infrastructure.py`, 外部ライブラリ |
| `presentation.py` | Discord Embed / View 表示など利用者向け UI 表現 | `models.py`, `domain.py`, `application.py`, `discord` |
| `__init__.py` | feature 外へ公開する API の再 export | 公開対象の層 |

依存方向は内側から外側へ逆流させない。`domain.py` から `discord`, `asyncpg`, `aiohttp`,
環境変数、`bot.py` のグローバル状態を参照しない。外部状態を読む必要がある場合は、
`bot.py` で snapshot を作るか、`application.py` に必要な値を引数として渡す。

### 現在の適用状況

- `dictionary`: built-in 読み辞書、英単語読み、顔文字辞書、辞書置換キャッシュ、
  `guild_dicts` / `builtin_reading_dicts` の SQL を所有。
  顔文字辞書は機械的なファイルサイズ分割ではなく、読みの意味に基づく
  `social`, `positive`, `negative`, `actions`, `slang`, `reactions` で分割する。
- `discord_bot`: Discord の slash command handler、lifecycle、message adapter、help embed、View/Modal を所有。
  Discord Bot であること自体を feature として扱う。
- `internal_tts_api`: 内部向け aiohttp API を所有。HTTP入口は feature だが、合成自体は
  `voice_synthesis` を呼び出す。
- `license`: `models.py` にライセンス/クレジット用データ、`domain.py` に話者名正規化と
  クレジット候補生成、`presentation.py` に Discord Embed 表示を配置。
- `mutes`: 読み上げミュート判定と `guild_mutes` の SQL を所有。
- `panel`: 現時点では表示 snapshot と Embed 生成のみなので、`models.py` と
  `presentation.py` の Stage 2 相当で維持。
- `status`: 公開してよい状態 snapshot と Embed 生成のみなので、`models.py` と
  `presentation.py` の Stage 2 相当で維持。
- `voice_playback`: 音声キュー、再生状態、Discord AudioSource 変換を所有。
- `voice_sessions`: VCセッション永続化と Discord VC 復旧 adapter を所有。
- `voice_settings`: ユーザー音声設定と `user_settings` の SQL を所有。
- `voice_synthesis`: TTSエンジン候補選定、HTTP合成、合成キャッシュを所有。

## 技術スタック

| 項目 | 技術 |
|---|---|
| 言語 | Python 3.12 |
| Discord ライブラリ | discord.py 2.7+ (voice extras) |
| コマンド体系 | 少数のスラッシュコマンド (`app_commands`) + 操作パネル UI |
| 音声合成 | VOICEVOX Engine (CPU版, Docker) |
| HTTP クライアント | aiohttp |
| DB | PostgreSQL + asyncpg |
| コンテナ | Docker Compose (ローカル / Coolify 本番) |
| CI | GitHub Actions (ruff + pyright + Pylance strict pyright + pytest) |
| ホットリロード | watchdog (watchmedo) |

## スラッシュコマンド

| コマンド | 説明 |
|---|---|
| `/vc` | VC接続/切断をトグル |
| `/panel` | 操作パネルを再投稿 |
| `/mute <user>` | 指定ユーザーの読み上げをミュート |
| `/unmute <user>` | 指定ユーザーのミュートを解除 |
| `/showmute` | ミュート中のユーザー一覧 |

接続/切断は `/vc` でも操作できる。スキップ、話者変更、音声設定、辞書、
状態確認、ライセンス確認は操作パネルのボタンに集約する。`/join` `/leave` は
登録せず、コマンドの入口を増やしすぎない。

## UI / UX 方針

- `/panel` を利用者の入口にし、接続、切断、スキップ、音声設定、話者変更、
  辞書、状態確認、ライセンス確認へボタンで移動できるようにする。
- command で VC を操作したい利用者向けには `/vc` トグルだけを残し、
  `/join` `/leave` は登録しない。
- 接続成功時はコマンド一覧ヘルプではなく、現在状態と操作案内を含む統合パネルを投稿する。
  `/panel` はその統合パネルの再投稿として扱う。
- 公開パネルやスラッシュコマンドには管理者向けデバッグ導線を置かない。
  直近エラーや trace ID などの詳細調査情報は Bot 運営側ログでのみ扱う。
- 接続、切断、スキップはそれぞれ独立したボタンにし、現在の接続・再生状態に応じて
  押せない操作を disabled にする。
- ボタン配置は「主操作」「個人設定」「確認・更新」に分け、危険操作である切断と
  スキップは danger style にする。
- 個人設定や状態確認は ephemeral 表示を基本とし、読み上げ対象チャンネルのノイズを抑える。
- 話者変更はエンジン → キャラクター → スタイルの順で選択し、複数音声エンジン対応を
  ユーザーに分かりやすく見せる。候補が多い場合は 25 件ずつページングする。
- 辞書UIはページングと選択式削除を持ち、登録数が増えても操作しやすくする。

## 運用・調査

- ログは `event=... fields={...}` の形式でイベント名を固定し、検索しやすくする。
- 主なログフィールドは `trace_id`, `guild_id`, `channel_id`, `user_id`,
  `speaker_id`, `queue_length`, `latency_ms`, `error`。
- 操作パネルの状態表示は、接続状態、読み上げ対象チャンネル、キュー長、話者数、
  エンジン取得状態のみを表示する。
- 直近エラー、trace ID、DB状態、Botインスタンスなどの内部情報は利用者向けUIに表示しない。

## ライセンス・権利対応

- 操作パネルのライセンス導線で VOICEVOX / COEIROINK / SHAREVOX の公式URL・規約URL・表記例を表示する。
- 現在のユーザー設定に基づくクレジット候補も同じ導線で表示する。候補はエンジン名の
  重複を避けるため、内部表示名の `[ENGINE]` 接頭辞を外して生成する。
- Bot側の表示は案内であり、実際の利用可否・クレジット条件・禁止用途は
  各公式サイトおよび各話者の規約を確認する。

## データ永続化

### DB スキーマ

```sql
-- ギルドごとのユーザー音声設定
CREATE TABLE user_settings (
    guild_id BIGINT NOT NULL DEFAULT 0,
    user_id BIGINT NOT NULL,
    speaker_id INTEGER NOT NULL DEFAULT 46,
    speed REAL NOT NULL DEFAULT 1.0,
    pitch REAL NOT NULL DEFAULT 0.0,
    intonation REAL NOT NULL DEFAULT 1.0,
    volume REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (guild_id, user_id)
);

-- ギルドごとの読み上げ辞書
CREATE TABLE guild_dicts (
    guild_id BIGINT NOT NULL,
    word TEXT NOT NULL,
    reading TEXT NOT NULL,
    PRIMARY KEY (guild_id, word)
);

-- ギルドごとのミュート設定
CREATE TABLE guild_mutes (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- built-in 読み辞書（起動時にメモリへロード）
CREATE TABLE builtin_reading_dicts (
    dict_type TEXT NOT NULL,
    word TEXT NOT NULL,
    reading TEXT NOT NULL,
    PRIMARY KEY (dict_type, word),
    CHECK (dict_type IN ('jp', 'en'))
);

-- 再起動・切断時に元の VC へ復旧するためのアクティブセッション
CREATE TABLE active_voice_sessions (
    guild_id BIGINT PRIMARY KEY,
    voice_channel_id BIGINT NOT NULL,
    text_channel_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 適用済みマイグレーション管理
CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### キャッシュ戦略

- 起動時に DB から全件ロードしてメモリキャッシュ
- `builtin_reading_dicts` は「保存先を DB、実行時はメモリ参照」の方針
- `apply_dict` はギルドごとにコンパイル済み正規表現を保持（辞書更新時に無効化）
- 定型文合成は LRU キャッシュ（in-flight 制御で同時重複合成を抑止）
- HTTP は共有 `aiohttp.ClientSession` で Keep-Alive 再利用
- テキスト前処理は fast-path ガードで不要な regex/emoji 置換をスキップ

### 読み辞書（built-in / user）の関係

- **適用順序**: `apply_dict`（ユーザ辞書）→ `apply_reading_corrections`（built-in）の順。ユーザ辞書で先に置換し、その後 built-in が拾えなかった部分を補正する。読みが既にカナ化された箇所は built-in も多くは noop。
- **ユーザ辞書 vs built-in の重複防止**:
  - 登録時 (`add_dict_entry`): 単語+読みが built-in と完全一致する登録は拒否し、ephemeral で「ビルドインと完全一致するため登録不要（読みを変えれば登録可能）」を返す
  - 起動時 (`purge_builtin_duplicates_from_user_dicts`): 既存ユーザ辞書から built-in 完全一致エントリを一括削除。ビルドイン拡充への自動追従。失敗しても on_ready は止めない
  - 1文字でも違えば登録可（ユーザのオーバーライド意図を尊重）。英語キーは case-insensitive 比較
- **CJK 互換単位記号の自動展開** ([builtin_readings.py](../bot/features/dictionary/builtin_readings.py)):
  - U+3300–U+33FF の Squared Katakana words (㌔→キロ、㍉→ミリ、㍍→メートル等) は `unicodedata.NFKC` で自動生成
  - Latin に分解されるもの (㎐→Hz、㎏→kg 等) は TTS engine 依存を避けるため、`_CJK_COMPAT_LATIN_UNIT_READINGS` で日本語カナ表記を手書き登録（ヘルツ/キログラム/ヘクタール等 約77件）
- **ネット略語の正規化方針**:
  - `XD` `(爆)` `(苦笑)` `🤣` などは「だいわらい」「ばくわら」「くわら」のような造語/略語を避け、**おおわらい / ばくしょう / にがわらい** のような標準日本語表記に統一
  - 「草」は「くさ」と「わらい」の両義あるため変換せず TTS の自然読み（くさ）に委ねる。`www` `ｗｗ` のみ「わらい」化（曖昧性なし）

### 起動シーケンス

`on_ready` では以下の順で初期化を実施する。

1. （`RUN_DB_MIGRATIONS=1` の時のみ）`migrations/runner.py` で未適用マイグレーションを実行（`schema_migrations` 管理）
2. `init_db` で必要テーブルを保証
3. `load_builtin_reading_dicts` で built-in 辞書を DB + デフォルトから再構築
4. ユーザー設定・ギルド辞書・ミュートをメモリへロード
5. `purge_builtin_duplicates_from_user_dicts` で、ビルドインと**単語+読み完全一致**するユーザー辞書を一括削除（ビルドイン拡充時の冗長エントリを掃除）。DB 操作失敗時は warning ログのみ出して on_ready の後続処理を巻き添えにしない
6. スラッシュコマンド同期・スピーカー取得
7. `_restore_voice_sessions_on_startup` で `active_voice_sessions` を読み、再起動前に接続していた VC へ順次再接続

### VC セッション復旧

デプロイ・プロセス再起動後に元の VC へ自動復帰する。さらに `bgm-bot` と同じ考え方で、Discord gateway の一時障害や deploy 中の自己切断は復旧対象として扱い、パネル/コマンド操作や audit log 上の手動切断は復旧しない。誤復帰を避けるため、自己切断時に即 DB を消すのではなく、短時間だけ auto-reconnect を待ってから切断理由を分類する。

- パネルの接続成功時に `active_voice_sessions` へ UPSERT、パネルの切断、全員退出、Bot がギルドから外れる時は DELETE
- 起動時: `on_ready` 末尾で `_spawn_background(_restore_voice_sessions_on_startup())` を 1 回だけ発火し、全 session を順次（並列度1で rate limit 安全側）に再接続
- gateway 復帰時: `discord.client` / `discord.gateway` の 5xx と `session has been invalidated` を検出し、次の `on_ready` で少し待ってから `_restore_voice_sessions_on_startup` を再実行する。Discord 側の voice state が落ち着く前に接続し直す race を避けるため遅延 task にしている
- ランタイム切断（`on_voice_state_update` で Bot 自身が VC から外れた）: `_cleanup_guild_playback_state` で queue/locks のみクリアし `read_channels` は保持（discord.py auto-reconnect 後の TTS 継続用）。DB session は即削除せず `_self_voice_recovery_tasks` で guild 単位に復旧判定を走らせる
- auto-reconnect 成功時: 復帰済み `voice_client` を検出したら queue を作り直し、`read_channels` と `active_voice_sessions` を再反映する。`read_channels` が無い場合は DB session から text channel を補う
- 復旧対象切断: パネル/コマンド由来の `_record_user_requested_disconnect` が直近になく、gateway 復旧対象ログがある、または audit log で手動切断と判定できない場合は保存 session へ `_reconnect_vc` で復旧する。audit log 権限が無い場合は deploy/network 側に倒して session を温存する
- **手動切断/kick の動作**: 直近のユーザー操作、または `member_disconnect` audit log（count=1、短時間内）を検出した場合は `_safe_forget_voice_session` と `_cleanup_guild_state` で session を削除し、次起動時 restore 対象外にする
- **graceful deploy の動作**: `TtsClient.close()` で `_shutting_down=True` を立て、終了処理中の自己 VC 切断は無視する。DB session を消さないため、次起動時 `_restore_voice_sessions_on_startup` で復帰できる
- **プロセス即死（deploy/crash）の動作**: `on_voice_state_update` が発火する前に process が消えるため DB session 削除は走らない → DB に session 残存 → 次起動時 `_restore_voice_sessions_on_startup` で復帰
- 起動時 restore は接続前に以下をチェックし、満たさなければ復旧せず DB から session 削除:
    - **VC が存在する**（削除されていない・型が VoiceChannel）
    - **部屋に non-bot メンバーが1人以上いる**（無音 VC で待機しても TTS する相手がいないため）
    - **Bot に Connect / Speak 権限がある**（接続後の発言不可状態を避ける、無駄なリトライ回避）
- 接続試行は指数バックオフ（2→4→8→16→32秒、最大 60秒、5回上限）。同一 guild の多重起動は `_vc_reconnect_inflight` でガード
- 全リトライ失敗で諦めた場合も DB から session を削除し、次回起動時に再試行ループに入らないようにする
- キュー（音声バッファ）は memory のみで永続化しない。再起動時はキュー空の状態で復帰する

複数Botモード（`DISCORD_TOKENS`）では、親プロセスがマイグレーションを1回実行してから子プロセスを起動する。
親は子プロセスを監視し、終了を検知すると指数バックオフ（1→2→4→8→16秒、上限60秒）で再起動する。
複数Bot同時クラッシュ時は各 slot の backoff 期限を統合し、最も近い期限まで一括 sleep する設計のため、復旧時間が台数に対して線形に伸びない。
5分以内に5回終了したインスタンスはクラッシュループとして親も停止（fail-fast）し、コンテナレベルのオートヒール（`restart: unless-stopped` 等）に委ねる。
SIGTERM/SIGINT 受信時は再起動を抑制し、全子プロセスへ SIGTERM を伝播する（10秒以内に終了しなければ SIGKILL）。
ログには `[bot#<index>]` を付与してインスタンスを識別できる。

## 音声合成フロー

```
テキストメッセージ受信
  ↓
辞書で単語置換 (apply_dict)
  ↓
100文字超は切り詰め
  ↓
POST /audio_query?text=...&speaker=ID → 読み上げパラメータ取得
  ↓
ユーザーの音声設定を適用 (speed, pitch, intonation, volume)
  ↓
outputSamplingRate=48000 / outputStereo=true を指定
  ↓
POST /synthesis?speaker=ID (JSON body) → WAV バイナリ取得
  ↓
Discord互換WAVなら PCMAudio で直接再生
（非互換時は FFmpegPCMAudio にフォールバック）
  ↓
ボイスチャンネルで再生
```

## 安定性対策

- ギルドごとに再生ロックを持ち、`play_next` の多重起動を防止
- Bot自身の VC 切断イベントで状態（キュー/ロック/通知時刻）をクリーンアップ
- Bot再接続時にキューが残っていれば再生を自動再開
- TTS接続エラー通知はギルド単位でレート制限
- Discord API 503 などログイン失敗時は指数バックオフで再試行
- トークン無効化（`LoginFailure` / WebSocket close 4004）検知時は永続的失敗として扱い、`TOKEN_INVALID_BACKOFF_SECONDS`（既定300秒）待機してから exit する。コンテナ即再起動による fast restart loop を緩和し、ログ汚染とクォータ消費を抑える。Discord Developer Portal でトークン再生成 → 環境変数更新 → redeploy が必要
- 複数Botモードで `DISCORD_TOKENS` 内の1トークンのみ無効化された場合、その子プロセスは300秒間隔でクラッシュ→再起動を繰り返すが、親のクラッシュループ検出（300秒に5回）には到達しないため他の正常な子プロセスは影響を受けず動作継続する
- VC 状態判定は `_has_active_voice_connection`（`guild.voice_client` 存在 + `is_connected()` 真）で統一し、stale な voice_client 残骸があってもパネルの接続/切断操作が破綻しないようにしている。inactive 分岐では `_reset_voice_state` で残骸の disconnect とメモリ状態の完全クリアを行ってから次の処理へ移る

## 環境変数

| 変数 | 説明 | デフォルト |
|---|---|---|
| `DISCORD_TOKEN` | 単一Bot起動時の Discord Bot トークン | - |
| `DISCORD_TOKENS` | 複数Bot起動時の Discord Bot トークン群（カンマ/改行区切り） | - |
| `VOICEVOX_URL` | VOICEVOX Engine の URL | `http://localhost:50021` |
| `COEIROINK_URL` | COEIROINK Engine の URL（Docker Compose では `http://coeiroink:50031`） | - |
| `SHAREVOX_URL` | SHAREVOX Engine の URL（Docker Compose では `http://sharevox:50025`） | - |
| `DEFAULT_SPEAKER_ID` | デフォルト Speaker ID（46=小夜/SAYO ノーマル） | `46` |
| `DATABASE_URL` | PostgreSQL 接続 URL | - |
| `LOG_LEVEL` | ログレベル | `INFO` |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | asyncpg コネクションプールサイズ（複数Bot時は max を絞ること） | `1` / `5` |
| `BOT_INSTANCE_INDEX` | （内部用）ログ識別子。複数Bot時は親が子へ自動付与 | `1` |
| `MULTIBOT_CHILD` | （内部用）子プロセス判定フラグ。親が `1` を渡す | `0` |
| `RUN_DB_MIGRATIONS` | （内部用）`0` で起動時マイグレーションを抑止。子プロセスでは自動的に `0` | `1` |

## ローカル開発

```bash
cp .env.example .env
# 単一なら DISCORD_TOKEN、複数なら DISCORD_TOKENS を設定
docker compose up
```

- `docker-compose.override.yml` が自動マージされ、ホットリロード・ポート公開が有効になる
- VOICEVOX: `localhost:50021`、PostgreSQL: `localhost:5432` でアクセス可能
- `COMPOSE_PROFILES=coeiroink,sharevox` を指定すると、COEIROINK: `localhost:50031`、SHAREVOX: `localhost:50025` も公開される

## Coolify デプロイ

### サービス構成

| サービス | 設定 |
|---|---|
| **Bot** | `docker-compose.yml` の `voicevox-discord` サービス。`bot/Dockerfile` でビルド |
| **PostgreSQL** | `postgres` サービス。`pgdata` ボリュームで永続化 |
| **VOICEVOX** | `voicevox` サービス。`voicevox/voicevox_engine:cpu-latest` を利用 |
| **COEIROINK v1** | 任意。`COMPOSE_PROFILES=coeiroink` で有効化し、`engines/coeiroink/Dockerfile` でビルド |
| **SHAREVOX** | 任意。`COMPOSE_PROFILES=sharevox` で有効化し、`engines/sharevox/Dockerfile` でビルド |

### Bot の環境変数

| 変数 | 値 |
|---|---|
| `DISCORD_TOKEN` または `DISCORD_TOKENS` | Discord Developer Portal から取得（複数運用は `DISCORD_TOKENS`） |
| `VOICEVOX_URL` | `http://voicevox:50021` |
| `COEIROINK_URL` | `http://coeiroink:50031` |
| `SHAREVOX_URL` | `http://sharevox:50025` |
| `DATABASE_URL` | `postgresql://bot:bot@postgres:5432/voicevox_bot` |

### COEIROINK v1 の有効化

Coolify の環境変数に `COMPOSE_PROFILES=coeiroink` と `COEIROINK_URL=http://coeiroink:50031` を設定する。
`coeiroink` サービスはデフォルトで公式COEIROINKキャラクターを全件同梱する。
モデルzipを多数ダウンロードし、Python/Torch系依存も重いため、初回ビルドには時間とディスク容量が必要。

必要な話者だけに絞る場合は、公式ダウンロードページの `prefix` を使い、以下の build args を上書きする。

| build arg | 説明 |
|---|---|
| `COEIROINK_SPEAKER_SOURCE` | 公式ダウンロードページまたは `downloadableSpeakers` を含む JSON |
| `COEIROINK_SPEAKER_PREFIXES` | 同梱する話者 prefix。空なら全件。複数指定は空白/カンマ区切り |

### SHAREVOX の有効化

Coolify の環境変数に `COMPOSE_PROFILES=sharevox` と `SHAREVOX_URL=http://sharevox:50025` を設定する。
COEIROINK と併用する場合は `COMPOSE_PROFILES=coeiroink,sharevox` にする。
`sharevox` サービスは SHAREVOX Engine / Core / 公式モデルを同梱する。
モデルzipをダウンロードするため、初回ビルドには時間とディスク容量が必要。

| build arg | 説明 |
|---|---|
| `SHAREVOX_ENGINE_REF` | `sharevox_engine` のタグまたはブランチ |
| `SHAREVOX_ENGINE_VERSION` | Engine の表示バージョン |
| `SHAREVOX_RESOURCE_VERSION` | ダウンロードする話者情報リソースのバージョン |
| `SHAREVOX_CORE_VERSION` | ダウンロードする `sharevox_core` のバージョン |
| `SHAREVOX_MODEL_VERSION` | ダウンロードする公式モデルzipのバージョン |

### デプロイ手順

1. Coolify で GitHub リポジトリを Docker Compose アプリとして作成
2. `docker-compose.yml` を指定してサービスを作成
3. Bot の環境変数に `DISCORD_TOKEN` または `DISCORD_TOKENS` を設定
4. 必要に応じて `VOICEVOX_URL` や `DATABASE_URL` を本番環境向けに上書き
5. デプロイ

## クレジット

音声を利用する場合、各ボイスの利用規約に従ってクレジット表記が必要。

各ボイスおよびライセンスはこちら:

- VOICEVOX: https://voicevox.hiroshiba.jp/ / https://voicevox.hiroshiba.jp/term/
- COEIROINK: https://coeiroink.com/ / https://coeiroink.com/terms
- SHAREVOX: https://sharevox.app/
