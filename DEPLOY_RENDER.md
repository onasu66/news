# Render へのデプロイ手順

## 前提

- プロジェクトを GitHub にプッシュ済みであること
- **newsite フォルダをリポジトリのルート**としてデプロイするか、Render の Root Directory を `newsite` に設定すること

## 手順

### 1. Render にサインアップ

https://render.com でアカウント作成し、GitHub と連携します。

### 2. New Web Service

1. **Dashboard** → **New +** → **Web Service**
2. 対象のリポジトリを選択
3. **Root Directory**: リポジトリ直下に newsite がある場合は `newsite` を指定
4. **Runtime**: Python
5. **Build Command**: `pip install -r requirements.txt`（既定のまま）
6. **Start Command**: `bash start.sh`（または `uvicorn main:app --host 0.0.0.0 --port $PORT`）

### 3. 環境変数

**Environment** タブで以下を設定します。

| 変数名 | 必須 | 説明 |
|--------|------|------|
| `OPENAI_API_KEY` | △ | AI解説に使う（Render で RSS/AI 無効なら不要）。未設定だと「APIキーが設定されていません」と表示される場合あり |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | ○* | Firebase サービスアカウント JSON 文字列（記事・解説の永続化） |
| `SITE_URL` | △ | サイトの絶対URL（例: `https://xxx.onrender.com`）。sitemap・OG・canonical用。未設定時はリクエストから自動取得 |
| `ADMIN_SECRET` | △ | 管理画面ログイン用。未設定なら管理機能は無効 |
| `DISABLE_RSS_AND_AI` | - | `1` または `true` で RSS 取得・AI 要約を無効（表示のみ）。**Render では未設定でも無効になる場合あり**（`RENDER` が設定されている場合） |
| `NEWS_REFRESH_INTERVAL` | - | ニュース更新間隔（分）。既定: 240 |
| `FULLTEXT_RSS_BASE_URL` | - | FiveFilters Full-Text RSS の URL（任意） |

\* **Render では必ず設定してください。** Render のディスクは一時的（エフェメラル）なため、SQLite のデータは再デプロイ・再起動で消えます。未設定だと「記事が一つも出てこない」状態になります。

### 4. デプロイ

**Create Web Service** をクリックしてデプロイを開始します。

### Blueprint を使う場合

`render.yaml` を使う場合は：

1. **New +** → **Blueprint**
2. リポジトリを選択
3. Root Directory を `newsite` に設定（必要な場合）
4. 環境変数は後から **Environment** で追加

## Render では RSS 取得・AI 要約は実行されません

- **Render 上では RSS からの記事取得と AI 要約は行いません**（`RENDER=true` が自動で設定されるため、または `DISABLE_RSS_AND_AI=1` を設定した場合）。
- 表示されるのは **Firestore 等に既に保存されている記事のみ**です。RSS 取得・記事生成は**ローカルや別サーバー・cron で実行**し、同じ Firestore を使う想定です。
- 手動の「RSS取得」「1本追加」「シード記事」などの API を叩いても、Render 上では 503 で拒否されます。
- トレンド取得と「ニュース更新」ボタンは動作しますが、記事リストの再取得はキャッシュのみ（RSS は呼びません）。

## 注意事項

- **ストレージ（記事が出てこない場合）**: Render のファイルシステムは**一時的**です。`FIREBASE_SERVICE_ACCOUNT_JSON` を設定しないと SQLite を使いますが、**再デプロイ・再起動のたびにデータが消え、記事が 0 件**になります。記事を表示したい場合は **Firestore 用の環境変数を必ず設定**してください。
- **コールドスタート**: 無料プランではアクセスがないとスリープします。初回アクセスは数十秒かかることがあります。
