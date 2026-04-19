# VOICEVOX 読み上げ Discord Bot - アーキテクチャ

## 概要

Discord のテキストチャンネルに投稿されたメッセージを、VOICEVOX の音声でボイスチャンネルに読み上げる Bot。
ギルドごとに独立したユーザー音声設定・辞書・ミュートを管理する。

## システム構成

```
┌──────────────────────────────────────────────────┐
│  Railway / Docker Compose                        │
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
│   ├── Dockerfile            ← 本番用 (Railway)
│   ├── Dockerfile.dev        ← 開発用 (watchdog ホットリロード)
│   ├── railway.toml          ← Railway サービス設定
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
| コンテナ | Docker Compose (ローカル) / Railway (本番) |
| CI | GitHub Actions (ruff + pytest) |
| ホットリロード | watchdog (watchmedo) |

## スラッシュコマンド

| コマンド | 説明 |
|---|---|
| `/join` | ユーザーがいるボイスチャンネルに接続 |
| `/leave` | ボイスチャンネルから切断 |
| `/vc` | VCに接続/切断をトグル |
| `/speaker <character> [style]` | 読み上げキャラクターを変更（style省略時: ノーマル、オートコンプリート対応） |
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
    speaker_id INTEGER NOT NULL DEFAULT 3,
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

### 起動シーケンス

`on_ready` では以下の順で初期化を実施する。

1. （`RUN_DB_MIGRATIONS=1` の時のみ）`migrate.py` で未適用マイグレーションを実行（`schema_migrations` 管理）
2. `init_db` で必要テーブルを保証
3. `load_builtin_reading_dicts` で built-in 辞書を DB + デフォルトから再構築
4. ユーザー設定・ギルド辞書・ミュートをメモリへロード
5. スラッシュコマンド同期・スピーカー取得

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

## 環境変数

| 変数 | 説明 | デフォルト |
|---|---|---|
| `DISCORD_TOKEN` | 単一Bot起動時の Discord Bot トークン | - |
| `DISCORD_TOKENS` | 複数Bot起動時の Discord Bot トークン群（カンマ/改行区切り） | - |
| `VOICEVOX_URL` | VOICEVOX Engine の URL | `http://localhost:50021` |
| `COEIROINK_URL` | COEIROINK Engine の URL（省略可） | - |
| `SHAREVOX_URL` | SHAREVOX Engine の URL（省略可） | - |
| `DEFAULT_SPEAKER_ID` | デフォルト Speaker ID | `3` |
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

## Railway デプロイ

### サービス構成

| サービス | 設定 |
|---|---|
| **Bot** | Source: GitHub リポジトリ、Root Directory: `bot/`、Dockerfile ビルド |
| **PostgreSQL** | Railway プラグインとして追加。`DATABASE_URL` が Bot に自動注入される |
| **VOICEVOX** | Docker Image: `voicevox/voicevox_engine:cpu-latest` として追加 |

### Bot の環境変数 (Railway Variables)

| 変数 | 値 |
|---|---|
| `DISCORD_TOKEN` または `DISCORD_TOKENS` | Discord Developer Portal から取得（複数運用は `DISCORD_TOKENS`） |
| `VOICEVOX_URL` | `http://voicevox.railway.internal:50021` |
| `DATABASE_URL` | PostgreSQL プラグインから自動注入 |

### デプロイ手順

1. Railway で新規プロジェクト作成
2. PostgreSQL プラグインを追加
3. VOICEVOX サービスを追加（Docker Image: `voicevox/voicevox_engine:cpu-latest`）
4. Bot サービスを追加（GitHub リポジトリ連携、Root Directory: `bot/`）
5. Bot の環境変数に `DISCORD_TOKEN` または `DISCORD_TOKENS` と `VOICEVOX_URL` を設定
6. デプロイ（`DATABASE_URL` は PostgreSQL プラグインから自動注入）

## クレジット

VOICEVOX で生成した音声を利用する場合、利用規約によりクレジット表記が必要。

> 「VOICEVOX:ずんだもん」
