# Firebase（Firestore）セットアップ

記事・解説データを Firestore に永続化するための手順です。Render など一時ストレージ環境で記事を保持したい場合に必要です。

## 1. Firebase プロジェクト作成

1. [Firebase Console](https://console.firebase.google.com/) にログイン
2. **プロジェクトを追加** で新規プロジェクト作成
3. **Firestore Database** を有効化（テストモードで開始でOK、本番時はルールを厳格化）

## 2. サービスアカウントキー取得

1. プロジェクトの **歯車** → **プロジェクトの設定**
2. **サービス アカウント** タブ
3. **新しい秘密鍵の生成** をクリック → JSON をダウンロード
4. ダウンロードした JSON を開き、**その内容を1行の文字列として** 環境変数 `FIREBASE_SERVICE_ACCOUNT_JSON` に設定

## 3. 環境変数の設定

### ローカル（.env）

```
FIREBASE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"..."}
```

※ JSON 内に改行・余分な空白がないこと。改行を含む場合は `\n` でエスケープするか、1行に圧縮してください。

### Render

1. **Environment** タブ
2. **FIREBASE_SERVICE_ACCOUNT_JSON** を追加
3. 値にサービスアカウント JSON の**全文**を貼り付け（Secret として保存推奨）

JSON が長いため、Render の「Bulk Edit」で貼り付けるか、ダッシュボードから直接入力してください。

## 4. 認証情報の読み込み順

1. **FIREBASE_SERVICE_ACCOUNT_JSON** 環境変数（Render 等で使用）
2. **credentials/firebase-service-account.json** ファイル（ローカル用。このファイルは .gitignore 済み）

いずれかが設定されていれば Firestore を使用します。どちらも未設定の場合は SQLite にフォールバックします。

## 5. Firestore コレクション

自動で以下のコレクションが作成・使用されます。

| コレクション | 用途 | 主なフィールド |
|-------------|------|----------------|
| `articles` | 掲載記事 | id, title, link, summary, published, source, category, image_url, added_at (Timestamp), has_explanation (bool) |
| `explanations` | AI 解説キャッシュ | article_id, inline_blocks, personas (配列), created_at (Timestamp) |

- `added_at` / `created_at` は Firestore Timestamp 型
- `has_explanation` は解説保存時に article に付与し、get_cached_article_ids の効率化に利用

## 6. 既存データの移行

Firestore にすでにデータがある場合、新形式（Timestamp, personas 配列, has_explanation）への移行が必要になることがあります。新規デプロイの場合は不要です。
