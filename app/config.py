"""アプリケーション設定"""
import os
from pathlib import Path

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    with open(_env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


class Settings:
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    CDN_BASE_URL: str = os.getenv("CDN_BASE_URL", "https://picsum.photos")
    NEWS_REFRESH_INTERVAL: int = int(os.getenv("NEWS_REFRESH_INTERVAL", "240"))
    # 一覧メモリキャッシュを DB と照合する間隔（分）。0 で無効。無料枠向け既定は 180。
    NEWS_LIST_CACHE_SYNC_MINUTES: int = int(os.getenv("NEWS_LIST_CACHE_SYNC_MINUTES", "180"))
    DAILY_ARTICLE_LIMIT: int = int(os.getenv("DAILY_ARTICLE_LIMIT", "6"))
    # 1回の RSS 強制更新で目標とする最小追加本数（無料枠向けに小さめ）
    RSS_MIN_ADDED_PER_REFRESH: int = int(os.getenv("RSS_MIN_ADDED_PER_REFRESH", "5"))
    # 上記を満たすまで process を繰り返す上限（無料枠向け既定は 1）
    RSS_REFRESH_MAX_LOOPS: int = int(os.getenv("RSS_REFRESH_MAX_LOOPS", "1"))
    # process_new_rss_articles 1回あたりの処理候補上限（無料枠向け既定は 10）
    RSS_PROCESS_MAX_PER_BATCH: int = int(os.getenv("RSS_PROCESS_MAX_PER_BATCH", "10"))
    # 論文: 各ドメイン（心理学・AI・Nature 等）から選ぶ本数
    RSS_PAPERS_PER_DOMAIN: int = int(os.getenv("RSS_PAPERS_PER_DOMAIN", "4"))
    # 論文: 1バッチで選ぶ合計の上限（max_per_run との小さい方が効く）
    RSS_MAX_TOTAL_PAPERS_PER_RUN: int = int(os.getenv("RSS_MAX_TOTAL_PAPERS_PER_RUN", "32"))
    # 一般ニュースRSS: この時間より古い published のエントリは候補から除外（フィードの日付表記に依存）
    RSS_NEWS_MAX_AGE_HOURS: int = int(os.getenv("RSS_NEWS_MAX_AGE_HOURS", "48"))
    # RSS 各フィードから読むエントリ上限・マージ後の候補プール上限
    RSS_ENTRIES_PER_FEED: int = int(os.getenv("RSS_ENTRIES_PER_FEED", "150"))
    RSS_FETCH_MAX_ITEMS: int = int(os.getenv("RSS_FETCH_MAX_ITEMS", "1200"))
    # PubMed 検索 RSS の limit= パラメータ
    RSS_PUBMED_FEED_LIMIT: int = int(os.getenv("RSS_PUBMED_FEED_LIMIT", "120"))
    # 論文記事として採用する最小要約文字数（短すぎる抄録は除外）
    RSS_MIN_PAPER_SUMMARY_CHARS: int = int(os.getenv("RSS_MIN_PAPER_SUMMARY_CHARS", "340"))
    # 論文トップ「すべて」で読み込む上限（解説付き論文のみの専用クエリ。既定2万・最大5万）
    PAPERS_LIST_MAX: int = int(os.getenv("PAPERS_LIST_MAX", "20000"))
    # Firestore 論文一覧（/ トップ）のメモリキャッシュ秒。記事保存・解説保存で無効化される
    PAPERS_SITE_LIST_CACHE_TTL_SEC: int = int(os.getenv("PAPERS_SITE_LIST_CACHE_TTL_SEC", "120"))
    # get_news 等で「解説付き ∩ articles」から並べる最大件数（既定は実質無制限に近い）
    NEWS_LIST_DISPLAY_MAX: int = int(os.getenv("NEWS_LIST_DISPLAY_MAX", "500000"))
    # 記事詳細の解説をメモリに保持する最大件数（LRU）。大きいほど Firestore 再読みが減る
    EXPLANATION_MEMORY_CACHE_MAX: int = int(os.getenv("EXPLANATION_MEMORY_CACHE_MAX", "10000"))
    # SQLite の load_all 上限（Firestore は全件スナップショット）
    SQLITE_ARTICLES_LIST_LIMIT: int = int(os.getenv("SQLITE_ARTICLES_LIST_LIMIT", "100000"))
    # Full-Text RSS 使用時に本文をどれだけ優先するか（0.0〜1.0）
    FULLTEXT_BODY_PRIORITY: float = float(os.getenv("FULLTEXT_BODY_PRIORITY", "0.85"))
    # FiveFilters Full-Text RSS のベースURL（未設定なら通常のRSSをそのまま取得）
    FULLTEXT_RSS_BASE_URL: str = os.getenv("FULLTEXT_RSS_BASE_URL", "").rstrip("/")
    # 管理者用シークレット（手動記事追加・管理画面）。未設定なら管理機能は利用不可
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "").strip()
    # Firebase（未設定ならSQLiteを使用）。サービスアカウントJSON文字列 or 空
    FIREBASE_SERVICE_ACCOUNT_JSON: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    # サイトの絶対URL（sitemap・OG・canonical用）。未設定時はリクエストの base_url を使用
    SITE_URL: str = os.getenv("SITE_URL", "").rstrip("/")
    # RapidAPI（Super Duper Trends 等）。未設定ならGoogleトレンドRSSのみ使用
    RAPIDAPI_KEY: str = os.getenv("RAPIDAPI_KEY", "").strip()
    RAPIDAPI_SUPER_DUPER_HOST: str = os.getenv("RAPIDAPI_SUPER_DUPER_HOST", "super-duper-trends.p.rapidapi.com").strip()
    # OpenAI による記事選定（キュレーション）。true にすると RSS 候補を AI で評価してから記事化
    AI_CURATION_ENABLED: str = os.getenv("AI_CURATION_ENABLED", "false")
    # 選定に使うモデル（未設定なら OPENAI_MODEL と同じ gpt-4o-mini）
    OPENAI_CURATION_MODEL: str = os.getenv("OPENAI_CURATION_MODEL", "")

settings = Settings()


def is_rss_and_ai_disabled() -> bool:
    """RSS取得・AI要約をこのインスタンスで無効にするか。

    既定は「有効」。無料枠運用でも定時更新を止めないため、
    明示的に DISABLE_RSS_AND_AI=true を指定した場合のみ無効化する。
    """
    v = os.getenv("DISABLE_RSS_AND_AI", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    return False
