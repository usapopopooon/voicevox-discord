# VOICEVOX 読み上げ Discord Bot

Discord のテキストチャンネルのメッセージを VOICEVOX の音声でボイスチャンネルに読み上げる Bot です。

## 機能

- テキストチャンネルのメッセージを自動読み上げ
- ギルドごとに独立したユーザー音声設定（キャラクター・話速・音高・抑揚・音量）
- ギルドごとの読み上げ辞書（単語→読みの置換）
- built-in 読み辞書（日本語難読語・英単語）を DB 保存し、起動時にメモリへロード
- ユーザーのミュート機能
- VC の入退室通知
- 全員退出時の自動切断
- URL・メールアドレスの自動省略
- 複数エンジン対応（VOICEVOX / COEIROINK / SHAREVOX）
- Bot 再接続時の読み上げ再開、TTS障害通知のレート制限

## コマンド一覧

| コマンド | 説明 |
|---|---|
| `/join` | ボイスチャンネルに接続 |
| `/leave` | ボイスチャンネルから切断 |
| `/vc` | 接続/切断をトグル |
| `/speaker <character> [style]` | 読み上げキャラクターを変更（style 省略時: ノーマル） |
| `/voice` | 話速・音高・抑揚・音量を変更 |
| `/skip` | 現在の読み上げをスキップ |
| `/dict` | 読み上げ辞書の設定 |
| `/mute <user>` | ユーザーの読み上げをミュート |
| `/unmute <user>` | ミュートを解除 |
| `/showmute` | ミュート中のユーザー一覧 |

## ローカル開発

```bash
cp .env.example .env
# .env に DISCORD_TOKEN（または DISCORD_TOKENS）を記入
docker compose up
```

### 複数Bot起動

- 単一Bot: `DISCORD_TOKEN=<token>`
- 複数Bot: `DISCORD_TOKENS=<token1>,<token2>,...`
- 複数運用時は `DISCORD_TOKEN` を空にして `DISCORD_TOKENS` のみ設定
- 両方指定した場合は両方のトークンが起動対象になります（重複は自動除外）

複数Botモードでは Bot プロセスをトークン数ぶん自動起動します。  
DB マイグレーションは親プロセスで1回だけ実行されます。  
SIGTERM/SIGINT 受信時は親が全子プロセスへ SIGTERM を伝播し、10秒以内に終了しなければ SIGKILL します。

`.env` 設定例:

```env
# 単一Bot
DISCORD_TOKEN=token1

# 複数Bot
# DISCORD_TOKEN=
# DISCORD_TOKENS=token1,token2,token3
```

## Railway デプロイ

1. Railway で新規プロジェクト作成
2. PostgreSQL プラグインを追加
3. VOICEVOX サービスを追加（Docker Image: `voicevox/voicevox_engine:cpu-latest`）
   - Internal Networking を有効化
4. Bot サービスを追加（GitHub リポジトリ連携、Root Directory: `bot/`）
5. Bot の環境変数を設定:
   - `DISCORD_TOKEN` または `DISCORD_TOKENS` — Discord Developer Portal から取得
   - `VOICEVOX_URL` — `http://<voicevoxサービス名>.railway.internal:50021`
   - `DATABASE_URL` — PostgreSQL プラグインから自動注入

## 技術スタック

- Python 3.12 / discord.py (voice)
- VOICEVOX Engine (CPU版)
- PostgreSQL + asyncpg
- Docker Compose (ローカル) / Railway (本番)
- GitHub Actions (ruff + pytest)

## 実装上のポイント

- 共有 `aiohttp.ClientSession` による HTTP 接続再利用
- 入退室通知など定型文の音声合成 LRU キャッシュ
- Discord 互換 WAV は `PCMAudio` で直接再生（非対応時のみ FFmpeg フォールバック）
- ギルド単位キュー + 再生ロックで多重再生競合を防止
- 起動時に DB マイグレーションを自動実行（`schema_migrations` で適用履歴管理）
- メッセージ前処理は fast-path ガードで不要な regex/emoji 置換をスキップ

詳細は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。

## クレジット

> VOICEVOX:ずんだもん
