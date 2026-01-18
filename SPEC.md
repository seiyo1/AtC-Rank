# 仕様書（詳細）

## 1. 概要
AtCoder Problems API から提出(AC)を検知し、精進スコアを算出して週間ランキングを表示する Discord Bot。

## 2. 対象
- Discord サーバー1つ（`GUILD_ID`固定）
- 登録ユーザーのみ対象
- AtCoder Problems 掲載の全問題
- ACのみカウント
- データ保存: SQLite

## 3. スコア計算
### 3.1 difficulty補正
```
if difficulty_raw < 400:
    display = round(400.0 / exp(1.0 - difficulty_raw/400.0))
else:
    display = round(difficulty_raw)
```

### 3.2 基本スコア
```
exponent = (rating - display_difficulty) / 400.0
score = 500.0 / (1.0 + exp(exponent))
score = round(score)
```

### 3.3 difficulty未設定
- 固定 **150点**

### 3.4 ストリーク倍率（線形）
```
mult = 1 + min(streak, 7) * 0.05
score_final = round(score_base * mult)
```

## 4. ストリーク
- JST日付で判定
- 同日: 維持
- 連続日: +1
- 途切れ: 1

## 5. 重複AC防止
- 同一問題のACは直近7日以内なら無視
- スコア・ストリーク・通知の全て対象外

## 6. ランキング
- 週間ランキング（JST月曜07:00開始）
- 加点のたびに固定メッセージ更新
- 同点は到達時刻が早い順（先着）
- 表示は全員（2000文字超は省略）
- 週間リセット時に前週ランキングを通知（ランキング埋め込みと同一デザイン）

## 7. 通知
- AC検知時にメンション付きで投稿
- スコア帯でテンプレ分岐（低/中/高/最高）
- 20%でAI文面に置換（`gpt-5-nano`、失敗時テンプレへ復帰）
- 週間リセット時のAIコメントは別モデルを指定可能（`AI_MODEL_WEEKLY`）

### 色・絵文字
- 絵文字はAC通知のみ
- 灰⬜ / 茶🟫 / 緑🟩 / 水💧 / 青🫐 / 黄🟨 / 橙🟧 / 赤🟥

### 埋め込み色
- difficultyがある場合のみAtCoder色に合わせた埋め込み色を付与
- difficulty未設定のときは色なし
- ダークテーマ寄りのRGB:
  - 灰 (192,192,192) / 茶 (176,140,86) / 緑 (63,175,63) / 水 (66,224,224)
  - 青 (136,136,255) / 黄 (255,255,86) / 橙 (255,184,54) / 赤 (255,103,103)

## 8. ロール付与
### 8.1 週間ロール
- 🏆 Weekly Champion
- 週次で1位に付与、前週分は剥奪

### 8.2 ストリークロール
- 🔥 7-Day Streak
- `current_streak >= 7` で付与、7未満で剥奪

### 8.3 レート色ロール
- ⬜ Gray / 🟫 Brown / 🟩 Green / 💧 Cyan / 🫐 Blue / 🟨 Yellow / 🟧 Orange / 🟥 Red
- 自動作成・付与／旧色ロールは外す
- 付与タイミング: `/register` と週1レート更新
- Botに「ロール管理」権限が必要

## 9. コマンド
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

## 10. スケジュール
- ポーリング: 3分（`POLL_INTERVAL_SECONDS`で変更可）
- 週間更新: JST月曜07:00
- レート更新: 週1回（週間更新時）
- Problems同期: 6時間ごと（`PROBLEMS_SYNC_INTERVAL_SECONDS`で変更可）
- ヘルスチェック: 6時間ごと（`HEALTHCHECK_INTERVAL_SECONDS`で変更可）
- 初回取得の起点: `INITIAL_FETCH_EPOCH`（デフォルト 1768748400 = 2026-01-19 00:00 JST）

## 11. データ設計（主要テーブル）
- users / problems / ratings / weekly_scores / submissions / streaks / user_problem_last_ac / settings / role_colors / weekly_reports

## 12. 未実装/暫定
- ランキング全員表示の完全版（2000文字制限対応は未実装）

## 13. ログ
- コンソールとファイルに出力（ローテーションあり）
- `LOG_FILE` / `LOG_LEVEL` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` で設定
