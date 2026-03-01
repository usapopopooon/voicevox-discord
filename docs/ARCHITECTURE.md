# VOICEVOX 読み上げ Discord Bot - アーキテクチャ

## 概要

Discord のテキストチャンネルに投稿されたメッセージを、VOICEVOX の音声でボイスチャンネルに読み上げる Bot。
ユーザーごとにキャラクターや音声パラメータを設定でき、ギルドごとに読み上げ辞書を管理できる。

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
| Discord ライブラリ | discord.py 2.6.4 (voice extras) |
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
| `/speaker` | 読み上げキャラクターを変更（オートコンプリート対応） |
| `/voice` | 音声パラメータを変更（話速・音高・抑揚・音量） |
| `/dict` | ギルド辞書の設定（ボタン UI で追加・削除） |

## データ永続化

### DB スキーマ

```sql
-- ユーザーごとの音声設定
CREATE TABLE user_settings (
    user_id BIGINT PRIMARY KEY,
    speaker_id INTEGER NOT NULL DEFAULT 3,
    speed REAL NOT NULL DEFAULT 1.0,
    pitch REAL NOT NULL DEFAULT 0.0,
    intonation REAL NOT NULL DEFAULT 1.0,
    volume REAL NOT NULL DEFAULT 1.0
);

-- ギルドごとの読み上げ辞書
CREATE TABLE guild_dicts (
    guild_id BIGINT NOT NULL,
    word TEXT NOT NULL,
    reading TEXT NOT NULL,
    PRIMARY KEY (guild_id, word)
);
```

### キャッシュ戦略

- 起動時に DB から全件ロードしてメモリキャッシュ
- 読み取りはメモリから（レイテンシ回避）
- 書き込みはメモリ更新 + DB に UPSERT/DELETE

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
POST /synthesis?speaker=ID (JSON body) → WAV バイナリ取得
  ↓
FFmpegPCMAudio で PCM 変換 → ボイスチャンネルで再生
```

## 環境変数

| 変数 | 説明 | デフォルト |
|---|---|---|
| `DISCORD_TOKEN` | Discord Bot トークン (必須) | - |
| `VOICEVOX_URL` | VOICEVOX Engine の URL | `http://localhost:50021` |
| `VOICEVOX_SPEAKER_ID` | デフォルト Speaker ID | `3` |
| `DATABASE_URL` | PostgreSQL 接続 URL | - |

## ローカル開発

```bash
cp .env.example .env
# .env に DISCORD_TOKEN を記入
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
| `DISCORD_TOKEN` | Discord Developer Portal から取得 |
| `VOICEVOX_URL` | `http://voicevox.railway.internal:50021` |
| `DATABASE_URL` | PostgreSQL プラグインから自動注入 |

### デプロイ手順

1. Railway で新規プロジェクト作成
2. PostgreSQL プラグインを追加
3. VOICEVOX サービスを追加（Docker Image: `voicevox/voicevox_engine:cpu-latest`）
4. Bot サービスを追加（GitHub リポジトリ連携、Root Directory: `bot/`）
5. Bot の環境変数に `DISCORD_TOKEN` と `VOICEVOX_URL` を設定
6. デプロイ（`DATABASE_URL` は PostgreSQL プラグインから自動注入）

## クレジット

VOICEVOX で生成した音声を利用する場合、利用規約によりクレジット表記が必要。

> 「VOICEVOX:ずんだもん」