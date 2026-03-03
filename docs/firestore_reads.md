# Firestore 読み取りが発生するタイミング

無料枠（読み取り 5万/日）を超えないよう、**いつ・どこで** Firestore が読まれるかを整理しました。

---

## 読み取りが発生する処理

| 処理 | 読むもの | 回数 | いつ呼ばれるか |
|------|----------|------|----------------|
| **get_cached_article_ids()** | メタ doc `_meta/cache` | **1読** | 一覧取得・sitemap・API状態・RSS処理時 |
| **load_all()** | articles コレクション（limit 2000） | **最大 2000 読** | 一覧取得・sitemap・API状態・RSS処理・翻訳処理時 |
| **load_by_id(article_id)** | 記事 1 ドキュメント | **1読/件** | 記事詳細で「一覧に無い記事」を開いたときだけ |
| **get_cached(article_id)** | explanations 1 ドキュメント | **1読/件** | 記事詳細ページを開くたび（解説取得） |

---

## すでにキャッシュされている部分

- **一覧（get_news）**  
  `NewsAggregator._news_cache` に保持しているため、**force_refresh しない限り** 2 回目以降の一覧表示では Firestore を読まない。  
  （初回・再起動直後・force_refresh 時だけ `get_cached_article_ids` + `load_all` が走る）

- **記事本文（get_article）**  
  一覧に載っている記事は `_news_cache` から返すので、**その記事の詳細を開く限り** `load_by_id` は呼ばれない。  
  一覧に無い URL を直で開いたときだけ `load_by_id` で 1 読。

---

## キャッシュで減らしている／これから減らす部分

- **get_cached_article_ids**  
  メタ 1 ドキュメントだけ読む形にしてあり、**explanations の全件ストリームは廃止**済み。  
  さらに **メモリで 60 秒キャッシュ** し、短時間に何度呼ばれても 1 回の Firestore 読取にまとめる。

- **get_cached(解説)**  
  記事詳細を開くたび 1 読していた部分を、**メモリキャッシュ（件数上限付き）** で減らす。  
  同じ記事を何度開いても、キャッシュヒット中は Firestore を読まない。

- **sitemap / api_status**  
  可能な範囲で **NewsAggregator.get_news()** を使い、一覧用の Firestore 読取を「既にメモリにあるときは発生しない」ようにする。

---

## 1 日あたりの読み取りイメージ（目安）

- 一覧はメモリキャッシュのため、**再起動や force_refresh の回数 × (1 + 2000)** 程度。
- 記事詳細は **「ユニークに開かれた記事数」＋ 解説のキャッシュミス分**。
- sitemap / 状態API は **get_news 経由にすれば** 一覧キャッシュヒット時は 0 読。

キャッシュを効かせることで、「同じ記事の再表示」や「短時間の連続アクセス」で Firestore を読む回数を抑えられます。
