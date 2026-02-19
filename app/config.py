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


settings = Settings()
