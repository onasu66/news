# 知リポAI

歴史上の偉人をモチーフにした14人のAIキャラクターが、最新のAI論文・研究・ニュースを多角的に解説する日本語ニュースメディアです。

**サイト: https://tiripo-ai.site**

- AI論文・研究解説: https://tiripo-ai.site/ai
- ニュース一覧: https://tiripo-ai.site/news
- 解説キャラクター一覧: https://tiripo-ai.site/personas

## 特徴

arXiv・Nature・Science などの論文と、国内外のニュースを毎日収集し、「1分で理解」と「詳しく読む」の2段階で解説します。同じ話題をブッダ・ニーチェ・織田信長・アインシュタインといった異なる視点のキャラクターが論じるため、一つの出来事を複数の角度から読めます。

## 技術構成

| 領域 | 採用技術 |
|---|---|
| アプリケーション | Python / FastAPI |
| テンプレート | Jinja2 |
| データストア | Turso (libSQL) |
| 記事生成 | Claude / Gemini |
| ホスティング | Render |

- `app/routers/` — ルーティング
- `app/services/` — 記事収集・生成・SEO・ストレージ
- `app/templates/` — Jinja2 テンプレート
- `config/` — キーワード方針などの設定

## 開発

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

環境変数は `DEPLOY_RENDER.md` を参照してください。
