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
| `OPENAI_API_KEY` | ○ | OpenAI API キー（AI解説に必要）。未設定だと「APIキーが設定されていません」と表示される |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | ○* | Firebase サービスアカウント JSON 文字列（記事・解説の永続化） |
| `SITE_URL` | △ | サイトの絶対URL（例: `https://xxx.onrender.com`）。sitemap・OG・canonical用。未設定時はリクエストから自動取得 |
| `ADMIN_SECRET` | △ | 管理画面ログイン用。未設定なら管理機能は無効 |
| `NEWS_REFRESH_INTERVAL` | - | ニュース更新間隔（分）。既定: 240 |
| `FULLTEXT_RSS_BASE_URL` | - | FiveFilters Full-Text RSS の URL（任意） |

\* `FIREBASE_SERVICE_ACCOUNT_JSON` を設定すると Firestore を使用し、再デプロイ後も記事が保持されます。未設定の場合は SQLite（一時的）を使用します。

### 4. デプロイ

**Create Web Service** をクリックしてデプロイを開始します。

### Blueprint を使う場合

`render.yaml` を使う場合は：

1. **New +** → **Blueprint**
2. リポジトリを選択
3. Root Directory を `newsite` に設定（必要な場合）
4. 環境変数は後から **Environment** で追加

## 注意事項

- **ストレージ**: `FIREBASE_SERVICE_ACCOUNT_JSON` を設定すれば Firestore に永続化され、再デプロイ後も記事・解説は保持されます。未設定の場合は SQLite を使いますが、無料プランでは再デプロイ時に消えます。
- **コールドスタート**: 無料プランではアクセスがないとスリープします。初回アクセスは数十秒かかることがあります。
