# 「APIキーが設定されていません」と表示される原因

## 原因

環境変数 **`OPENAI_API_KEY`** が未設定または空のとき、AI解説を生成できず「APIキーが設定されていません」というメッセージが表示されます。

## 解決方法

### ローカル

1. プロジェクト直下に `.env` を作成
2. 次を記述（値は実際の API キーに置き換える）:
   ```
   OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
   ```

### Render

1. Render ダッシュボード → 対象の Web Service
2. **Environment** タブを開く
3. **Add Environment Variable**
4. Key: `OPENAI_API_KEY`、Value: あなたの OpenAI API キー
5. **Save Changes** で再デプロイ

## 補足

- API キーは [OpenAI Platform](https://platform.openai.com/api-keys) で取得できます
- `.env` は `.gitignore` に含まれているため、リポジトリには含まれません
- Render では **Secret** として保存することを推奨します
