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
│  │ discord-bot  │───→│ voicevox              │   │
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

- **discord-bot**: Bot 本体。スラッシュコマンドとメッセージ読み上げを処理
- **voicevox**: VOICEVOX Engine (CPU版)。テキスト→音声合成 API
- **PostgreSQL**: ユーザー設定・辞書の永続化

## ディレクトリ構成

```
voicevox-discord/
├── bot/
│   ├── bot.py                ← Bot 本体
│   ├── readings_builtin.py   ← built-in 読み辞書（JP/EN）
│   ├── kaomoji_builtin.py    ← built-in 顔文字辞書
│   ├── migrate.py            ← マイグレーションランナー
│   ├── migrations/           ← 逐次適用される DB マイグレーション
│   ├── Dockerfile            ← 本番用
│   ├── Dockerfile.dev        ← 開発用 (watchdog ホットリロード)
│   ├── requirements.txt      ← 本番依存
│   ├── requirements.dev.txt  ← 開発追加依存
│   └── tests/
│       ├── conftest.py       ← テスト用環境変数
│       └── test_bot.py       ← テスト
├── voicevox/
│   └── Dockerfile            ← VOICEVOX Engine ラッパー
├── docs/
│   └── ARCHITECTURE.md       ← このファイル
├── docker-compose.yml          ← 共通ベース定義
├── docker-compose.override.yml ← ローカル開発用上書き (自動適用)
├── pyproject.toml              ← ruff / pytest 設定
├── .github/workflows/ci.yml   ← GitHub Actions CI
├── .env.example                ← 環境変数テンプレート
├── .gitignore
└── .dockerignore
```

## 技術スタック

| 項目 | 技術 |
|---|---|
| 言語 | Python 3.12 |
| Discord ライブラリ | discord.py 2.7+ (voice extras) |
| コマンド体系 | スラッシュコマンド (`app_commands`) |
| 音声合成 | VOICEVOX Engine (CPU版, Docker) |
| HTTP クライアント | aiohttp |
| DB | PostgreSQL + asyncpg |
| コンテナ | Docker Compose (ローカル / Coolify 本番) |
| CI | GitHub Actions (ruff + pytest) |
| ホットリロード | watchdog (watchmedo) |

## スラッシュコマンド

| コマンド | 説明 |
|---|---|
| `/join` | ユーザーがいるボイスチャンネルに接続 |
| `/leave` | ボイスチャンネルから切断 |
| `/vc` | VCに接続/切断をトグル |
| `/speaker <engine> <character> [style]` | 読み上げキャラクターを変更（engineで候補を絞り込み、style省略時は先頭スタイル） |
| `/voice` | 音声パラメータを変更（話速・音高・抑揚・音量） |
| `/skip` | 現在読み上げ中の音声をスキップ |
| `/mute <user>` | 指定ユーザーの読み上げをミュート |
| `/unmute <user>` | 指定ユーザーのミュートを解除 |
| `/showmute` | ミュート中のユーザー一覧 |
| `/dict` | ギルド辞書の設定（ボタン UI で追加・削除） |

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
- **CJK 互換単位記号の自動展開** ([readings_builtin.py](../bot/readings_builtin.py)):
  - U+3300–U+33FF の Squared Katakana words (㌔→キロ、㍉→ミリ、㍍→メートル等) は `unicodedata.NFKC` で自動生成
  - Latin に分解されるもの (㎐→Hz、㎏→kg 等) は TTS engine 依存を避けるため、`_CJK_COMPAT_LATIN_UNIT_READINGS` で日本語カナ表記を手書き登録（ヘルツ/キログラム/ヘクタール等 約77件）
- **ネット略語の正規化方針**:
  - `XD` `(爆)` `(苦笑)` `🤣` などは「だいわらい」「ばくわら」「くわら」のような造語/略語を避け、**おおわらい / ばくしょう / にがわらい** のような標準日本語表記に統一
  - 「草」は「くさ」と「わらい」の両義あるため変換せず TTS の自然読み（くさ）に委ねる。`www` `ｗｗ` のみ「わらい」化（曖昧性なし）

### 起動シーケンス

`on_ready` では以下の順で初期化を実施する。

1. （`RUN_DB_MIGRATIONS=1` の時のみ）`migrate.py` で未適用マイグレーションを実行（`schema_migrations` 管理）
2. `init_db` で必要テーブルを保証
3. `load_builtin_reading_dicts` で built-in 辞書を DB + デフォルトから再構築
4. ユーザー設定・ギルド辞書・ミュートをメモリへロード
5. `purge_builtin_duplicates_from_user_dicts` で、ビルドインと**単語+読み完全一致**するユーザー辞書を一括削除（ビルドイン拡充時の冗長エントリを掃除）。DB 操作失敗時は warning ログのみ出して on_ready の後続処理を巻き添えにしない
6. スラッシュコマンド同期・スピーカー取得
7. `_restore_voice_sessions_on_startup` で `active_voice_sessions` を読み、再起動前に接続していた VC へ順次再接続

### VC セッション復旧

デプロイ・プロセス再起動後に元の VC へ自動復帰する。**ランタイム切断（モデレータ手動切断・ネットワーク断・権限剥奪等）は復旧対象外** とし、ユーザーが必要なら `/join` で再接続する設計。これにより audit log 権限要件や false-positive 復帰を回避する。

- `/join` 成功時に `active_voice_sessions` へ UPSERT、`/leave` `/vc(off)` `全員退出` `Bot がギルドから外れる` 時は DELETE
- 起動時: `on_ready` 末尾で `_spawn_background(_restore_voice_sessions_on_startup())` を発火し、全 session を順次（並列度1で rate limit 安全側）に再接続
- ランタイム切断（`on_voice_state_update` で Bot 自身が VC から外れた）: `_cleanup_guild_playback_state` で queue/locks のみクリアし `read_channels` は保持（discord.py auto-reconnect 後の TTS 継続用）。**DB session は即座に `_safe_forget_voice_session` で削除**（手動切断/kick の場合は次起動時の意図しない rejoin を防ぐ）
- ランタイム再接続（`on_voice_state_update` で Bot が VC へ復帰）: `_safe_record_voice_session` で DB session を**再記録**する。これにより一時的ネットワーク断（WS 4006 等）で discord.py が auto-reconnect した場合、削除した DB session を再び記録し、後続のプロセス再起動時にも `_restore_voice_sessions_on_startup` で復帰できる。`read_channels` が無い場合（/leave 後等）は再記録しない
- **手動切断/kick の動作**: discord.py は auto-reconnect しないため再接続イベント発火なし → 切断時に削除された DB session が空のまま → 次起動時 restore 対象外 → bot は VC に戻らない
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
- VC 状態判定は `_has_active_voice_connection`（`guild.voice_client` 存在 + `is_connected()` 真）で統一し、stale な voice_client 残骸があっても `/vc` `/leave` `/join` の挙動が破綻しないようにしている。inactive 分岐では `_reset_voice_state` で残骸の disconnect とメモリ状態の完全クリアを行ってから次の処理へ移る

## 環境変数

| 変数 | 説明 | デフォルト |
|---|---|---|
| `DISCORD_TOKEN` | 単一Bot起動時の Discord Bot トークン | - |
| `DISCORD_TOKENS` | 複数Bot起動時の Discord Bot トークン群（カンマ/改行区切り） | - |
| `VOICEVOX_URL` | VOICEVOX Engine の URL | `http://localhost:50021` |
| `COEIROINK_URL` | COEIROINK Engine の URL（Docker Compose では `http://coeiroink:50031`） | - |
| `SHAREVOX_URL` | SHAREVOX Engine の URL（省略可） | - |
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

## Coolify デプロイ

### サービス構成

| サービス | 設定 |
|---|---|
| **Bot** | `docker-compose.yml` の `discord-bot` サービス。`bot/Dockerfile` でビルド |
| **PostgreSQL** | `postgres` サービス。`pgdata` ボリュームで永続化 |
| **VOICEVOX** | `voicevox` サービス。`voicevox/voicevox_engine:cpu-latest` を利用 |
| **COEIROINK v1** | 任意。`COMPOSE_PROFILES=coeiroink` で有効化し、`engines/coeiroink/Dockerfile` でビルド |

### Bot の環境変数

| 変数 | 値 |
|---|---|
| `DISCORD_TOKEN` または `DISCORD_TOKENS` | Discord Developer Portal から取得（複数運用は `DISCORD_TOKENS`） |
| `VOICEVOX_URL` | `http://voicevox:50021` |
| `COEIROINK_URL` | `http://coeiroink:50031` |
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

### デプロイ手順

1. Coolify で GitHub リポジトリを Docker Compose アプリとして作成
2. `docker-compose.yml` を指定してサービスを作成
3. Bot の環境変数に `DISCORD_TOKEN` または `DISCORD_TOKENS` を設定
4. 必要に応じて `VOICEVOX_URL` や `DATABASE_URL` を本番環境向けに上書き
5. デプロイ

## クレジット

音声を利用する場合、各ボイスの利用規約に従ってクレジット表記が必要。

各ボイスおよびライセンスはこちら:

- VOICEVOX: https://voicevox.hiroshiba.jp/
- COEIROINK: https://coeiroink.com/
