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
    DAILY_ARTICLE_LIMIT: int = int(os.getenv("DAILY_ARTICLE_LIMIT", "6"))
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

settings = Settings()


def is_rss_and_ai_disabled() -> bool:
    """RSS取得・AI要約をこのインスタンスで無効にするか。Render では True にするとRSS/AIを動かさず表示のみ。"""
    v = os.getenv("DISABLE_RSS_AND_AI", "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if os.getenv("RENDER", "").strip().lower() == "true":
        return True
    return False
