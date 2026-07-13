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
                k = k.strip()
                v = v.strip().strip('"\'')
                if not k:
                    continue
                # setdefault は「キーがあるが値が空文字」のときに .env を反映しないため、
                # 未設定または空のみ .env で上書きする（Render の非空環境変数は優先）
                prev = os.environ.get(k)
                if prev is None or (isinstance(prev, str) and prev.strip() == ""):
                    if v != "":
                        os.environ[k] = v


class Settings:
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # 記事化 AI プロバイダ: openai | gemini
    AI_PROVIDER: str = os.getenv("AI_PROVIDER", "openai").strip().lower()
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    # 軽量タスク用プール（ナビ・翻訳・タイトル等）。RPM はモデルごとに別枠
    GEMINI_MODEL_POOL_LITE: str = os.getenv(
        "GEMINI_MODEL_POOL_LITE",
        "gemini-2.5-flash-lite",
    ).strip()
    # 品質タスク用プール（本文・ミドルマン解説・ペルソナ）。頭の良いモデルを指定
    GEMINI_MODEL_POOL_QUALITY: str = os.getenv(
        "GEMINI_MODEL_POOL_QUALITY",
        "gemini-2.5-flash",
    ).strip()
    # 旧設定（未指定時の lite フォールバック）
    GEMINI_MODEL_POOL: str = os.getenv(
        "GEMINI_MODEL_POOL",
        "gemini-2.5-flash-lite,gemini-2.5-flash",
    ).strip()
    # 品質モデルが 429 のとき lite プールへフォールバックするか
    GEMINI_QUALITY_FALLBACK_LITE: str = os.getenv("GEMINI_QUALITY_FALLBACK_LITE", "true").strip().lower()
    # Gemini 記事化バッチ: 1件処理後に次の候補まで待つ秒数（RPM 対策）。0 で無効
    GEMINI_ARTICLE_INTERVAL_SEC: int = int(os.getenv("GEMINI_ARTICLE_INTERVAL_SEC", "45"))
    # Gemini 429 時に OpenAI へフォールバック（AI_PROVIDER=gemini 時。キーがあれば既定 ON）
    OPENAI_FALLBACK_ENABLED: str = os.getenv("OPENAI_FALLBACK_ENABLED", "").strip().lower()
    OPENAI_FALLBACK_MODEL: str = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4o-mini").strip()
    # 本文・ペルソナ等 quality タスクのフォールバック（未設定時は OPENAI_FALLBACK_MODEL）
    OPENAI_FALLBACK_QUALITY_MODEL: str = os.getenv("OPENAI_FALLBACK_QUALITY_MODEL", "gpt-4o-mini").strip()
    # ペルソナを OpenAI で出すときのモデル。空なら OPENAI_MODEL。gpt- 指定時は Gemini 設定でも OpenAI 直行
    OPENAI_PERSONA_COMMENT_MODEL: str = os.getenv("OPENAI_PERSONA_COMMENT_MODEL", "").strip()
    # ペルソナを Gemini で出すとき（空なら GEMINI_MODEL_POOL_QUALITY）
    PERSONA_GEMINI_MODEL: str = os.getenv("PERSONA_GEMINI_MODEL", "").strip()
    # ペルソナ連続生成の間隔（秒）。Gemini RPM 対策。0 で無効
    GEMINI_PERSONA_INTERVAL_SEC: int = int(os.getenv("GEMINI_PERSONA_INTERVAL_SEC", "8"))
    MIDDLEMAN_PROVIDER: str = os.getenv("MIDDLEMAN_PROVIDER", "claude_first").strip().lower()
    # MIDDLEMAN_PROVIDER=openai のときに使うモデル（未設定なら OPENAI_MODEL）
    MIDDLEMAN_OPENAI_MODEL: str = os.getenv("MIDDLEMAN_OPENAI_MODEL", "gpt-4o-mini").strip()
    # ペルソナコメントは OpenAI（または PERSONA_PROVIDER 指定）のみ。Claude CLI はリサーチ専用。
    _persona_provider_env = os.getenv("PERSONA_PROVIDER", "").strip().lower()
    PERSONA_PROVIDER: str = _persona_provider_env or "openai"
    # 互換のため残す（ペルソナの保存後 Claude は行わない）
    PERSONA_CLAUDE_AFTER_SAVE: str = os.getenv("PERSONA_CLAUDE_AFTER_SAVE", "true").strip().lower()
    # 記事化時のタイトル生成を有効化するか
    TITLE_GENERATION_ENABLED: str = os.getenv("TITLE_GENERATION_ENABLED", "true").strip().lower()
    # 記事化時タイトル生成に使うモデル
    TITLE_OPENAI_MODEL: str = os.getenv("TITLE_OPENAI_MODEL", "").strip()
    CDN_BASE_URL: str = os.getenv("CDN_BASE_URL", "https://picsum.photos")
    NEWS_REFRESH_INTERVAL: int = int(os.getenv("NEWS_REFRESH_INTERVAL", "240"))
    # 一覧メモリキャッシュを DB と照合する間隔（分）。0 で無効。本番 Render は別プロセスのため 30 分ごとの同期を推奨。
    NEWS_LIST_CACHE_SYNC_MINUTES: int = int(os.getenv("NEWS_LIST_CACHE_SYNC_MINUTES", "30"))
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
    # 1バッチあたり「一般ニュース」に最低確保する枠（論文だけで max_per_run を埋めない）
    RSS_MIN_NEWS_SLOTS_PER_RUN: int = int(os.getenv("RSS_MIN_NEWS_SLOTS_PER_RUN", "5"))
    # 一般ニュースRSS: この時間より古い published のエントリは候補から除外（フィードの日付表記に依存）
    RSS_NEWS_MAX_AGE_HOURS: int = int(os.getenv("RSS_NEWS_MAX_AGE_HOURS", "48"))
    # RSS 各フィードから読むエントリ上限・マージ後の候補プール上限
    RSS_ENTRIES_PER_FEED: int = int(os.getenv("RSS_ENTRIES_PER_FEED", "150"))
    RSS_FETCH_MAX_ITEMS: int = int(os.getenv("RSS_FETCH_MAX_ITEMS", "1200"))
    # PubMed 検索 RSS の limit= パラメータ
    RSS_PUBMED_FEED_LIMIT: int = int(os.getenv("RSS_PUBMED_FEED_LIMIT", "120"))
    # 論文記事として採用する最小要約文字数（短すぎる抄録は除外）
    RSS_MIN_PAPER_SUMMARY_CHARS: int = int(os.getenv("RSS_MIN_PAPER_SUMMARY_CHARS", "340"))
    # 記事化: タイトル+要約+（完全な）本文の合計最低文字数
    ARTICLE_MIN_SOURCE_CHARS: int = int(os.getenv("ARTICLE_MIN_SOURCE_CHARS", "400"))
    # 本文取得なし時の最低要約文字数（ニュース / 論文）
    ARTICLE_MIN_SUMMARY_CHARS: int = int(os.getenv("ARTICLE_MIN_SUMMARY_CHARS", "280"))
    ARTICLE_MIN_SUMMARY_CHARS_PAPER: int = int(os.getenv("ARTICLE_MIN_SUMMARY_CHARS_PAPER", "400"))
    # 完全な本文がこれ以上あれば要約が短くても可
    ARTICLE_MIN_BODY_CHARS: int = int(os.getenv("ARTICLE_MIN_BODY_CHARS", "200"))
    # 生成後: ミドルマン text ブロック合計の最低文字数（3分読了の目安）
    ARTICLE_MIN_GENERATED_TEXT_CHARS: int = int(os.getenv("ARTICLE_MIN_GENERATED_TEXT_CHARS", "600"))
    ARTICLE_MIN_JA_RATIO: float = float(os.getenv("ARTICLE_MIN_JA_RATIO", "0.35"))
    ARTICLE_MIN_EXPLAIN_COUNT: int = int(os.getenv("ARTICLE_MIN_EXPLAIN_COUNT", "3"))
    ARTICLE_MIN_NAVIGATOR_CHARS: int = int(os.getenv("ARTICLE_MIN_NAVIGATOR_CHARS", "500"))
    # Claude 選定 JSON の summary がこれ未満なら curated 読み込み時に除外（reason方式移行後は実質不使用）
    CURATED_MIN_SUMMARY_CHARS: int = int(os.getenv("CURATED_MIN_SUMMARY_CHARS", "0"))
    # Notion 連携（Claude 選定ログ）
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")
    # Xポスト書き込み先 Notion ページID（子ページが自動作成される）
    NOTION_XPOST_PAGE_ID: str = os.getenv("NOTION_XPOST_PAGE_ID", "")
    # 論文トップ「すべて」で読み込む上限（Neon/SQLite いずれも一覧の負荷に直結）
    PAPERS_LIST_MAX: int = int(os.getenv("PAPERS_LIST_MAX", "120"))
    # 論文一覧のメモリキャッシュ秒（アプリ側）
    PAPERS_SITE_LIST_CACHE_TTL_SEC: int = int(os.getenv("PAPERS_SITE_LIST_CACHE_TTL_SEC", "86400"))
    # get_news 等で並べる最大件数（既定は実質無制限に近い）
    NEWS_LIST_DISPLAY_MAX: int = int(os.getenv("NEWS_LIST_DISPLAY_MAX", "500000"))
    # 旧 Firestore メタ検知（互換のみ。未使用なら 0）
    NEWS_META_FP_POLL_SEC: float = float(os.getenv("NEWS_META_FP_POLL_SEC", "0"))
    # sync_list_cache で _news_cache が空のとき、差分復元に使う最大件数（全件 load_all 回避用）
    NEWS_SYNC_SEED_MAX: int = int(os.getenv("NEWS_SYNC_SEED_MAX", "50"))
    # 起動後の自動シード（RSS取得→記事化）を有効化するか（無料枠向け既定は無効）
    STARTUP_SEED_ENABLED: str = os.getenv("STARTUP_SEED_ENABLED", "false")
    # 記事詳細の解説をメモリに保持する最大件数（LRU）
    EXPLANATION_MEMORY_CACHE_MAX: int = int(os.getenv("EXPLANATION_MEMORY_CACHE_MAX", "10000"))
    # SQLite の load_all 上限
    SQLITE_ARTICLES_LIST_LIMIT: int = int(os.getenv("SQLITE_ARTICLES_LIST_LIMIT", "100000"))
    # Full-Text RSS 使用時に本文をどれだけ優先するか（0.0〜1.0）
    FULLTEXT_BODY_PRIORITY: float = float(os.getenv("FULLTEXT_BODY_PRIORITY", "0.85"))
    # FiveFilters Full-Text RSS のベースURL（未設定なら通常のRSSをそのまま取得）
    FULLTEXT_RSS_BASE_URL: str = os.getenv("FULLTEXT_RSS_BASE_URL", "").rstrip("/")
    # Neon Postgres 接続文字列（設定されていれば SQLite より優先）
    DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
    # 管理者用シークレット（手動記事追加・管理画面）。未設定なら管理機能は利用不可
    ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "").strip()
    # 本番キャッシュ更新通知専用（ローカル Cron 等 → SITE_URL の /api/admin/cache/refresh）。
    # 未設定時は従来どおり ADMIN_SECRET のみ。設定時は本番とローカルに同一文字列を置けば ADMIN_SECRET は別でも可。
    CACHE_REFRESH_SECRET: str = os.getenv("CACHE_REFRESH_SECRET", "").strip()
    # セッション Cookie 署名専用（未設定時は main.py で ADMIN_SECRET 派生鍵を使用）
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "").strip()
    # サイトの絶対URL（sitemap・OG・canonical・IndexNow用）。未設定時はリクエストの base_url を使用
    SITE_URL: str = os.getenv("SITE_URL", "").rstrip("/")
    # IndexNow（Bing等）: 8〜128文字の英数字・ハイフン。{SITE_URL}/{KEY}.txt に同一文字列を公開する
    INDEXNOW_KEY: str = os.getenv("INDEXNOW_KEY", "").strip()
    INDEXNOW_ENABLED: str = os.getenv("INDEXNOW_ENABLED", "true").strip().lower()
    GA4_MEASUREMENT_ID: str = os.getenv("GA4_MEASUREMENT_ID", "").strip()
    CLARITY_PROJECT_ID: str = os.getenv("CLARITY_PROJECT_ID", "").strip()
    # 公開HTML（/topic, /, /news）の Cache-Control max-age（秒）。0 でヘッダなし。CDN/ブラウザの再訪削減用。
    PUBLIC_HTML_CACHE_MAX_AGE_SEC: int = int(os.getenv("PUBLIC_HTML_CACHE_MAX_AGE_SEC", "120"))
    # RapidAPI（Super Duper Trends 等）。未設定ならGoogleトレンドRSSのみ使用
    RAPIDAPI_KEY: str = os.getenv("RAPIDAPI_KEY", "").strip()
    RAPIDAPI_SUPER_DUPER_HOST: str = os.getenv("RAPIDAPI_SUPER_DUPER_HOST", "super-duper-trends.p.rapidapi.com").strip()
    # OpenAI による記事選定（キュレーション）。true にすると RSS 候補を AI で評価してから記事化
    AI_CURATION_ENABLED: str = os.getenv("AI_CURATION_ENABLED", "false")
    # 選定に使うモデル（未設定なら OPENAI_MODEL と同じ gpt-4o-mini）
    OPENAI_CURATION_MODEL: str = os.getenv("OPENAI_CURATION_MODEL", "")
    # curated_history の保持件数（Claude選定の重複判定に使用）
    CURATED_HISTORY_MAX: int = int(os.getenv("CURATED_HISTORY_MAX", "300"))
    # curated_history の重複判定対象期間（日）。短くすると再採用されやすい
    CURATED_HISTORY_LOOKBACK_DAYS: int = int(os.getenv("CURATED_HISTORY_LOOKBACK_DAYS", "14"))

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
