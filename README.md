# atcrank

AtCoder Problems を使った精進ランキング Discord Bot。
AC検知 → スコア計算 → 週間ランキング更新 → 通知までを自動化します。

## 主な機能
- AC検知（AtCoder Problems API）
- 週間ランキング（JST 月曜 07:00 開始）を固定メッセージで常時更新
- 週間リセット時に前週ランキングを通知（ランキングと同一デザインの埋め込み）
- 精進スコア計算（difficulty補正 + ロジスティック式）
- 7日ローリングで同一問題の再加点を無視
- ストリーク（連続AC日数）
- 週間1位 / 7日ストリーク ロール付与
- レート色ロールを自動作成・付与
- AC通知（テンプレ文／20%でAI文面に置換）
- ヘルスチェック投稿（稼働状況の定期通知）

## 前提
- Python 3.11+
- SQLite（ローカルファイル）
- Discord Bot（サーバー管理者権限 or ロール管理権限）
- Discord Developers Portal で **Server Members Intent** を有効化

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

SQLiteのDBファイルは起動時に自動作成されます。

### .env 設定

必須:
- `DISCORD_TOKEN`
- `SQLITE_PATH`
- `GUILD_ID`

任意:
- `OPENAI_API_KEY`（AI文面を使う場合）
- `AI_ENABLED` / `AI_PROBABILITY`
- `POLL_INTERVAL_SECONDS`（デフォルト180）
- `INITIAL_FETCH_EPOCH`（初回取得の起点UNIX秒、デフォルト: 1768748400 = 2026-01-19 00:00 JST）
- `PROBLEMS_SYNC_INTERVAL_SECONDS`（デフォルト21600）
- `HEALTHCHECK_INTERVAL_SECONDS`（デフォルト21600）
- `LOG_LEVEL` / `LOG_FILE` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`

### 環境変数の詳細

**必須**
- `DISCORD_TOKEN`: Discord Botのトークン
- `SQLITE_PATH`: SQLite DBファイルパス（例: `atcrank.db`）
- `GUILD_ID`: 対象DiscordサーバーID（数値）

**AI（任意）**
- `OPENAI_API_KEY`: AI文面を使う場合のAPIキー
- `AI_ENABLED`: `true/false`（デフォルト: `true`）
- `AI_PROBABILITY`: AI文面を使う確率(%)（デフォルト: `20`）
- `AI_MODEL`: モデル名（デフォルト: `gpt-5-nano`）
- `AI_MODELS_NOTIFY`: 通知用の複数モデル（カンマ区切り、設定時は複数コメントを出力）
  - 未設定でも `AI_MODEL` がカンマ区切りなら通知に複数モデルを使用
  - 複数モデル時のみコメントに `[model-name]` が付き、1モデル時は従来どおり本文のみ
- `AI_MODEL_WEEKLY`: 週間リセット用AIモデル（デフォルト: `AI_MODEL`）

**スケジュール（任意）**
- `POLL_INTERVAL_SECONDS`: 提出ポーリング間隔（秒、デフォルト: `180`）
- `PROBLEMS_SYNC_INTERVAL_SECONDS`: Problems同期間隔（秒、デフォルト: `21600`）
- `HEALTHCHECK_INTERVAL_SECONDS`: ヘルスチェック投稿間隔（秒、デフォルト: `21600`）

**ログ（任意）**
- `LOG_LEVEL`: `DEBUG/INFO/WARNING/ERROR`（デフォルト: `INFO`）
- `LOG_FILE`: ログ出力先（デフォルト: `logs/atcrank.log`）
- `LOG_MAX_BYTES`: ローテーションのサイズ上限（デフォルト: `1048576`）
- `LOG_BACKUP_COUNT`: ローテーション保持数（デフォルト: `5`）

`.env` 例:
```
DISCORD_TOKEN=xxx
SQLITE_PATH=atcrank.db
GUILD_ID=1234567890
OPENAI_API_KEY=
AI_ENABLED=true
AI_PROBABILITY=20
AI_MODEL=gpt-5-nano
AI_MODELS_NOTIFY=
AI_MODEL_WEEKLY=gpt-5-mini
POLL_INTERVAL_SECONDS=180
INITIAL_FETCH_EPOCH=1768748400
PROBLEMS_SYNC_INTERVAL_SECONDS=21600
HEALTHCHECK_INTERVAL_SECONDS=21600
LOG_LEVEL=INFO
LOG_FILE=logs/atcrank.log
```

## 起動

```bash
python main.py
```

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

## 初期設定（Discord内で実行）
1. `/set_notify_channel`（AC通知のチャンネル）
2. `/set_rank_channel`（ランキング固定メッセージのチャンネル）
3. `/set_health_channel`（ヘルスチェック通知のチャンネル）
4. `/set_roles weekly_role streak_role`（手動作成したロールを指定）

## コマンド

### ユーザー
- `/register atcoder_id`
- `/unregister`
- `/ranking`
- `/profile`

### 管理者のみ
- `/register @user atcoder_id`
- `/unregister @user`
- `/set_notify_channel`
- `/set_rank_channel`
- `/set_health_channel`
- `/set_roles weekly_role streak_role`
- `/set_ai enabled probability`
- `/debug_notify`（通知デザインのプレビュー）
- `/debug_notify_ai`（AI通知プレビュー）
- `/debug_rank`（ランキングデザインのプレビュー）
- `/debug_weekly_reset`（週次リセット通知のプレビュー）
- `/debug_weekly_reset_ai`（AI付き週次リセット通知のプレビュー）

## 注意事項
- ランキング全員表示は 2000文字制限により省略される場合があります。
- 色ロールは自動作成されます（Botに「ロール管理」権限が必要）。
- 週間ロール/ストリークロールは手動で作成して `/set_roles` で紐づけてください。
- 週次リセット通知の内容はDBに保存されます（`weekly_reports`）。

## 仕様詳細
`SPEC.md` を参照してください。
