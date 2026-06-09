"""統計メトリクス収集サービス - 各省庁データを統一フォーマットで保存。

対応データソース:
  - e-Stat API（統計局）
  - 統計ダッシュボード API（e-Stat）
  - 財務省（CSV/Excel）
  - 厚生労働省（CSV/Excel）
  - 出入国在留管理庁（CSV）
  - 文部科学省（CSV/Excel）
  - 警察庁（CSV）
  - 国土交通省（API/CSV）
  - 内閣府（CSV/Excel）
  - ETL基盤（環境変数 ETL_API_URL 設定時）
"""
import csv
import io
import json
import logging
import os
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SQLITE_DB = PROJECT_ROOT / "data" / "metrics.db"


@dataclass
class MetricRow:
    category: str
    name: str
    value: Optional[float]
    unit: str = ""
    subcategory: str = ""
    year: Optional[int] = None
    month: Optional[int] = None
    region: str = ""
    source: str = ""
    source_url: str = ""


# ---------- DB 書き込み ----------

def _use_neon() -> bool:
    try:
        from .neon_store import use_neon
        return use_neon()
    except Exception:
        return False


def _sqlite_upsert(rows: list[MetricRow]) -> int:
    if not rows:
        return 0
    _SQLITE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SQLITE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            subcategory TEXT,
            name        TEXT NOT NULL,
            value       REAL,
            unit        TEXT,
            year        INTEGER,
            month       INTEGER,
            region      TEXT,
            source      TEXT,
            source_url  TEXT,
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_cat ON metrics(category, year DESC)")
    count = 0
    for r in rows:
        try:
            conn.execute(
                "INSERT INTO metrics (category, subcategory, name, value, unit, year, month, region, source, source_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r.category, r.subcategory, r.name, r.value, r.unit,
                 r.year, r.month, r.region, r.source, r.source_url),
            )
            count += 1
        except Exception as e:
            logger.debug("sqlite_upsert スキップ: %s - %s", r.name, e)
    conn.commit()
    conn.close()
    return count


def save_metrics(rows: list[MetricRow]) -> int:
    if not rows:
        return 0
    if _use_neon():
        try:
            from .neon_store import neon_metrics_upsert
            return neon_metrics_upsert([r.__dict__ for r in rows])
        except Exception as e:
            logger.warning("neon_metrics_upsert 失敗、SQLite に切替: %s", e)
    return _sqlite_upsert(rows)


# ---------- ユーティリティ ----------

def _fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ja,en;q=0.9",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_text(url: str, encoding: str = "utf-8", timeout: int = 30) -> str:
    return _fetch_url(url, timeout).decode(encoding, errors="replace")


def _safe_float(val) -> Optional[float]:
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(str(val).replace(",", "").replace("，", "").strip())
    except Exception:
        return None


# ---------- コレクター基底クラス ----------

class BaseCollector:
    source_name: str = ""
    category: str = ""

    def fetch(self) -> list[MetricRow]:
        raise NotImplementedError

    def collect_and_save(self) -> int:
        try:
            rows = self.fetch()
            if rows:
                saved = save_metrics(rows)
                logger.info("[%s] %d 件保存", self.source_name, saved)
                return saved
            logger.info("[%s] 取得件数 0", self.source_name)
            return 0
        except Exception as e:
            logger.warning("[%s] 収集失敗: %s", self.source_name, e)
            return 0


# ---------- e-Stat API ----------

ESTAT_API_KEY = os.environ.get("ESTAT_API_KEY", "")
ESTAT_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"

# 収集する統計表 ID と対応カテゴリ・名称
ESTAT_STATS_LIST = [
    # 人口動態調査: 出生数・死亡数・婚姻数
    {"stats_id": "0003411572", "category": "出生", "name_prefix": "出生数"},
    {"stats_id": "0003411573", "category": "婚姻", "name_prefix": "婚姻件数"},
    # 国勢調査: 総人口
    {"stats_id": "0003412313", "category": "人口", "name_prefix": "総人口"},
    # 労働力調査: 完全失業率
    {"stats_id": "0003437048", "category": "労働", "name_prefix": "完全失業率"},
]


class EstatCollector(BaseCollector):
    source_name = "e-Stat API"

    def fetch(self) -> list[MetricRow]:
        if not ESTAT_API_KEY:
            logger.info("[e-Stat] ESTAT_API_KEY 未設定。スキップします。")
            return []
        rows = []
        for spec in ESTAT_STATS_LIST:
            try:
                url = (
                    f"{ESTAT_BASE}/getStatsData"
                    f"?appId={ESTAT_API_KEY}&statsDataId={spec['stats_id']}"
                    f"&metaGetFlg=N&cntGetFlg=N&startPosition=1&limit=50&lang=J"
                )
                raw = _fetch_text(url)
                data = json.loads(raw)
                values = (data.get("GET_STATS_DATA", {})
                              .get("STATISTICAL_DATA", {})
                              .get("DATA_INF", {})
                              .get("VALUE", []))
                if isinstance(values, dict):
                    values = [values]
                for v in values[:50]:
                    val_text = v.get("$", "")
                    time_val = v.get("@time", "")
                    year = int(time_val[:4]) if len(time_val) >= 4 else None
                    month = int(time_val[4:6]) if len(time_val) >= 6 else None
                    rows.append(MetricRow(
                        category=spec["category"],
                        name=spec["name_prefix"],
                        value=_safe_float(val_text),
                        year=year,
                        month=month,
                        source="e-Stat",
                        source_url=f"https://www.e-stat.go.jp/stat-search/files?statInfId={spec['stats_id']}",
                    ))
            except Exception as e:
                logger.warning("[e-Stat] %s 取得失敗: %s", spec["stats_id"], e)
        return rows


# ---------- 統計ダッシュボード API ----------
# エンドポイント: getData
# 指標コードは getIndicatorInfo API で確認済み

DASHBOARD_BASE = "https://dashboard.e-stat.go.jp/api/1.0/Json/getData"

# 確認済み有効な指標コード（APIで実際に取得成功したもの）
DASHBOARD_INDICATORS = [
    # 総人口（人口推計） value例: 126749000 (人)
    {"indicator_code": "0201010000000010000", "category": "人口",  "name": "総人口", "unit": "人"},
    # 合計特殊出生率（人口動態調査） value例: 1.42
    {"indicator_code": "0203010010000020010", "category": "出生",  "name": "合計特殊出生率", "unit": ""},
    # 完全失業率（労働力調査） value例: 2.4 (%)
    {"indicator_code": "0301010000020020010", "category": "労働",  "name": "完全失業率", "unit": "%"},
    # 婚姻件数（人口動態調査） value例: 586481
    {"indicator_code": "0203070000000010010", "category": "婚姻",  "name": "婚姻件数", "unit": "件"},
]


class DashboardCollector(BaseCollector):
    source_name = "統計ダッシュボードAPI"

    def fetch(self) -> list[MetricRow]:
        rows = []
        for spec in DASHBOARD_INDICATORS:
            try:
                url = (
                    f"{DASHBOARD_BASE}"
                    f"?Lang=JP&IndicatorCode={spec['indicator_code']}"
                    f"&TimeFrom=2015CY00&TimeTo=2024CY00"
                    f"&Cycle=3&RegionalRank=2&IsSeasonalAdjustment=1"
                    f"&MetaGetFlg=N&SectionHeaderFlg=1"
                )
                raw = _fetch_text(url)
                data = json.loads(raw)
                stat = data.get("GET_STATS", {})
                # status=0 が正常終了
                if stat.get("RESULT", {}).get("status") != "0":
                    logger.warning("[DashboardAPI] %s: status=%s", spec["name"], stat.get("RESULT", {}).get("status"))
                    continue
                stat_data = stat.get("STATISTICAL_DATA", {})
                # DATA_OBJ が各データポイントの配列
                data_objs = stat_data.get("DATA_INF", {}).get("DATA_OBJ", [])
                if isinstance(data_objs, dict):
                    data_objs = [data_objs]
                for item in data_objs[:20]:
                    val_entry = item.get("VALUE", {})
                    time_str = str(val_entry.get("@time", ""))
                    year = int(time_str[:4]) if len(time_str) >= 4 else None
                    val_raw = val_entry.get("$", "")
                    rows.append(MetricRow(
                        category=spec["category"],
                        name=spec["name"],
                        value=_safe_float(val_raw),
                        unit=spec.get("unit", ""),
                        year=year,
                        source="統計ダッシュボード",
                        source_url="https://dashboard.e-stat.go.jp/",
                    ))
            except Exception as e:
                logger.warning("[DashboardAPI] %s 取得失敗: %s", spec["name"], e)
        return rows


# ---------- 財務省（e-Stat 経由） ----------
# 財務省の直接CSV URLは変更されやすいため e-Stat API から取得する

MOF_ESTAT_IDS = [
    # 財政統計（一般会計歳入歳出）
    {"stats_id": "0003123645", "category": "税収", "subcategory": "一般会計歳入", "name_prefix": "一般会計歳入"},
    # 国債残高
    {"stats_id": "0003240280", "category": "財政", "subcategory": "国債残高", "name_prefix": "国債残高"},
]


class MoFCollector(BaseCollector):
    source_name = "財務省"

    def fetch(self) -> list[MetricRow]:
        if not ESTAT_API_KEY:
            logger.info("[財務省] ESTAT_API_KEY 未設定のためスキップ")
            return []
        rows = []
        for spec in MOF_ESTAT_IDS:
            try:
                url = (
                    f"{ESTAT_BASE}/getStatsData"
                    f"?appId={ESTAT_API_KEY}&statsDataId={spec['stats_id']}"
                    f"&metaGetFlg=N&cntGetFlg=N&startPosition=1&limit=30&lang=J"
                )
                raw = _fetch_text(url)
                data = json.loads(raw)
                values = (data.get("GET_STATS_DATA", {})
                              .get("STATISTICAL_DATA", {})
                              .get("DATA_INF", {})
                              .get("VALUE", []))
                if isinstance(values, dict):
                    values = [values]
                for v in values[:30]:
                    time_val = v.get("@time", "")
                    year = int(time_val[:4]) if len(time_val) >= 4 else None
                    rows.append(MetricRow(
                        category=spec["category"],
                        subcategory=spec["subcategory"],
                        name=spec["name_prefix"],
                        value=_safe_float(v.get("$", "")),
                        unit="億円",
                        year=year,
                        source="財務省（e-Stat）",
                        source_url=f"https://www.e-stat.go.jp/stat-search/files?statInfId={spec['stats_id']}",
                    ))
            except Exception as e:
                logger.warning("[財務省] %s 取得失敗: %s", spec["stats_id"], e)
        return rows


# ---------- 厚生労働省（e-Stat 経由） ----------
# 直接URLはファイル年度ごとに変わるため e-Stat API から取得する

MHLW_ESTAT_IDS = [
    # 人口動態調査 出生・死亡数
    {"stats_id": "0003411572", "category": "出生", "name_prefix": "出生数（人口動態）"},
    # 人口推計（総務省 e-Stat）
    {"stats_id": "0003410767", "category": "人口", "name_prefix": "推計人口"},
    # 婚姻件数
    {"stats_id": "0003411573", "category": "婚姻", "name_prefix": "婚姻件数（人口動態）"},
]


class MHLWCollector(BaseCollector):
    source_name = "厚生労働省"

    def fetch(self) -> list[MetricRow]:
        if not ESTAT_API_KEY:
            logger.info("[厚生労働省] ESTAT_API_KEY 未設定のためスキップ")
            return []
        rows = []
        for spec in MHLW_ESTAT_IDS:
            try:
                url = (
                    f"{ESTAT_BASE}/getStatsData"
                    f"?appId={ESTAT_API_KEY}&statsDataId={spec['stats_id']}"
                    f"&metaGetFlg=N&cntGetFlg=N&startPosition=1&limit=30&lang=J"
                )
                raw = _fetch_text(url)
                data = json.loads(raw)
                values = (data.get("GET_STATS_DATA", {})
                              .get("STATISTICAL_DATA", {})
                              .get("DATA_INF", {})
                              .get("VALUE", []))
                if isinstance(values, dict):
                    values = [values]
                for v in values[:30]:
                    time_val = v.get("@time", "")
                    year = int(time_val[:4]) if len(time_val) >= 4 else None
                    month = int(time_val[4:6]) if len(time_val) >= 6 else None
                    rows.append(MetricRow(
                        category=spec["category"],
                        name=spec["name_prefix"],
                        value=_safe_float(v.get("$", "")),
                        year=year,
                        month=month,
                        source="厚生労働省（e-Stat）",
                        source_url=f"https://www.e-stat.go.jp/stat-search/files?statInfId={spec['stats_id']}",
                    ))
            except Exception as e:
                logger.warning("[厚生労働省] %s 取得失敗: %s", spec["stats_id"], e)
        return rows


# ---------- 出入国在留管理庁 ----------

class ImmigrationCollector(BaseCollector):
    source_name = "出入国在留管理庁"

    def fetch(self) -> list[MetricRow]:
        rows = []
        try:
            url = "https://www.moj.go.jp/isa/content/001393549.csv"
            text = _fetch_text(url, encoding="cp932")
            reader = csv.reader(io.StringIO(text))
            for i, row in enumerate(reader):
                if i < 1 or not row:
                    continue
                year_str = str(row[0]).replace("年末", "").strip()
                year = int(year_str) if year_str.isdigit() else None
                val = _safe_float(row[1]) if len(row) > 1 else None
                if year and val is not None:
                    rows.append(MetricRow(
                        category="外国人",
                        name="在留外国人数",
                        value=val,
                        unit="人",
                        year=year,
                        source="出入国在留管理庁",
                        source_url=url,
                    ))
        except Exception as e:
            logger.warning("[出入国在留管理庁] 取得失敗: %s", e)
        return rows


# ---------- 文部科学省 ----------

class MEXTCollector(BaseCollector):
    source_name = "文部科学省"

    def fetch(self) -> list[MetricRow]:
        rows = []
        try:
            url = "https://www.e-stat.go.jp/stat-search/file-download?statInfId=000031749675&fileKind=0"
            text = _fetch_text(url, encoding="cp932")
            reader = csv.reader(io.StringIO(text))
            for i, row in enumerate(reader):
                if i < 2 or not row:
                    continue
                year_str = str(row[0]).replace("年度", "").strip()
                year = int(year_str) if year_str.isdigit() else None
                val = _safe_float(row[1]) if len(row) > 1 else None
                if year and val is not None:
                    rows.append(MetricRow(
                        category="教育",
                        name="学校数（全学校種別合計）",
                        value=val,
                        unit="校",
                        year=year,
                        source="文部科学省",
                        source_url="https://www.mext.go.jp/b_menu/toukei/chousa01/kihon/1267995.htm",
                    ))
        except Exception as e:
            logger.warning("[文部科学省] 取得失敗: %s", e)
        return rows


# ---------- 警察庁 ----------

class NPACollector(BaseCollector):
    source_name = "警察庁"

    def fetch(self) -> list[MetricRow]:
        rows = []
        try:
            url = "https://www.npa.go.jp/publications/statistics/crime/data/h02_h30_sou.csv"
            text = _fetch_text(url, encoding="cp932")
            reader = csv.reader(io.StringIO(text))
            for i, row in enumerate(reader):
                if i < 1 or len(row) < 2:
                    continue
                year_str = str(row[0]).replace("年", "").strip()
                year = int(year_str) if year_str.isdigit() else None
                val = _safe_float(row[1])
                if year and val is not None:
                    rows.append(MetricRow(
                        category="犯罪",
                        name="刑法犯認知件数",
                        value=val,
                        unit="件",
                        year=year,
                        source="警察庁",
                        source_url=url,
                    ))
        except Exception as e:
            logger.warning("[警察庁] 取得失敗: %s", e)
        return rows


# ---------- 国土交通省 ----------

class MLITCollector(BaseCollector):
    source_name = "国土交通省"

    def fetch(self) -> list[MetricRow]:
        rows = []
        try:
            url = "https://www.e-stat.go.jp/stat-search/file-download?statInfId=000031524028&fileKind=0"
            text = _fetch_text(url, encoding="cp932")
            reader = csv.reader(io.StringIO(text))
            for i, row in enumerate(reader):
                if i < 2 or not row:
                    continue
                year_str = str(row[0]).replace("年度", "").strip()
                year = int(year_str) if year_str.isdigit() else None
                val = _safe_float(row[1]) if len(row) > 1 else None
                if year and val is not None:
                    rows.append(MetricRow(
                        category="住宅",
                        name="新設住宅着工戸数",
                        value=val,
                        unit="戸",
                        year=year,
                        source="国土交通省",
                        source_url="https://www.mlit.go.jp/statistics/details/jutaku_list.html",
                    ))
        except Exception as e:
            logger.warning("[国土交通省] 取得失敗: %s", e)
        return rows


# ---------- 内閣府 ----------

class CAOCollector(BaseCollector):
    source_name = "内閣府"

    # 内閣府SNA直接CSV URLは年度ごとに変わる → e-Stat API から GDP・経済指標を取得
    CAO_ESTAT_IDS = [
        # 国民経済計算（GDP等）- 内閣府 e-Stat
        {"stats_id": "0003109830", "category": "財政", "name_prefix": "名目GDP"},
        # 消費者物価指数
        {"stats_id": "0003143687", "category": "財政", "name_prefix": "消費者物価指数"},
    ]

    def fetch(self) -> list[MetricRow]:
        if not ESTAT_API_KEY:
            logger.info("[内閣府] ESTAT_API_KEY 未設定のためスキップ")
            return []
        rows = []
        for spec in self.CAO_ESTAT_IDS:
            try:
                url = (
                    f"{ESTAT_BASE}/getStatsData"
                    f"?appId={ESTAT_API_KEY}&statsDataId={spec['stats_id']}"
                    f"&metaGetFlg=N&cntGetFlg=N&startPosition=1&limit=30&lang=J"
                )
                raw = _fetch_text(url)
                data = json.loads(raw)
                values = (data.get("GET_STATS_DATA", {})
                              .get("STATISTICAL_DATA", {})
                              .get("DATA_INF", {})
                              .get("VALUE", []))
                if isinstance(values, dict):
                    values = [values]
                for v in values[:30]:
                    time_val = v.get("@time", "")
                    year = int(time_val[:4]) if len(time_val) >= 4 else None
                    rows.append(MetricRow(
                        category=spec["category"],
                        name=spec["name_prefix"],
                        value=_safe_float(v.get("$", "")),
                        unit="億円",
                        year=year,
                        source="内閣府（e-Stat）",
                        source_url=f"https://www.e-stat.go.jp/stat-search/files?statInfId={spec['stats_id']}",
                    ))
            except Exception as e:
                logger.warning("[内閣府] %s 取得失敗: %s", spec["stats_id"], e)
        return rows


# ---------- ETL基盤 ----------

class ETLCollector(BaseCollector):
    source_name = "ETL基盤"

    def fetch(self) -> list[MetricRow]:
        etl_url = os.environ.get("ETL_API_URL", "").strip()
        if not etl_url:
            logger.debug("[ETL] ETL_API_URL 未設定。スキップします。")
            return []
        rows = []
        try:
            raw = _fetch_text(etl_url)
            data = json.loads(raw)
            if not isinstance(data, list):
                data = [data]
            for item in data:
                rows.append(MetricRow(
                    category=item.get("category", "その他"),
                    subcategory=item.get("subcategory", ""),
                    name=item.get("name", ""),
                    value=_safe_float(item.get("value")),
                    unit=item.get("unit", ""),
                    year=item.get("year"),
                    month=item.get("month"),
                    region=item.get("region", ""),
                    source="ETL基盤",
                    source_url=etl_url,
                ))
        except Exception as e:
            logger.warning("[ETL] 取得失敗: %s", e)
        return rows


# ---------- 全コレクター実行 ----------

ALL_COLLECTORS: list[BaseCollector] = [
    EstatCollector(),
    DashboardCollector(),
    MoFCollector(),
    MHLWCollector(),
    ImmigrationCollector(),
    MEXTCollector(),
    NPACollector(),
    MLITCollector(),
    CAOCollector(),
    ETLCollector(),
]


def collect_all_metrics() -> int:
    """全コレクターを順に実行し、保存した総件数を返す。"""
    total = 0
    for collector in ALL_COLLECTORS:
        total += collector.collect_and_save()
    logger.info("collect_all_metrics 完了: 合計 %d 件保存", total)
    return total


def collect_metrics_by_category(categories: list[str]) -> int:
    """指定カテゴリに関連するコレクターのみ実行。"""
    category_collector_map = {
        "財政":   [MoFCollector(), CAOCollector()],
        "税収":   [MoFCollector()],
        "人口":   [EstatCollector(), DashboardCollector(), MHLWCollector()],
        "出生":   [EstatCollector(), DashboardCollector()],
        "婚姻":   [EstatCollector()],
        "労働":   [EstatCollector(), DashboardCollector()],
        "外国人": [ImmigrationCollector()],
        "住宅":   [MLITCollector()],
        "教育":   [MEXTCollector()],
        "犯罪":   [NPACollector()],
    }
    seen = set()
    total = 0
    for cat in categories:
        for collector in category_collector_map.get(cat, []):
            cid = id(collector.__class__)
            if cid not in seen:
                seen.add(cid)
                total += collector.collect_and_save()
    return total


# ---------- SQLite クエリ（Neon なし時のAPI用） ----------

def sqlite_metrics_query(
    category: str = "",
    subcategory: str = "",
    name: str = "",
    year: Optional[int] = None,
    limit: int = 200,
    offset: int = 0,
) -> list:
    if not _SQLITE_DB.exists():
        return []
    conn = sqlite3.connect(str(_SQLITE_DB))
    conds = []
    params = []
    if category:
        conds.append("category = ?")
        params.append(category)
    if subcategory:
        conds.append("subcategory = ?")
        params.append(subcategory)
    if name:
        conds.append("name LIKE ?")
        params.append(f"%{name}%")
    if year is not None:
        conds.append("year = ?")
        params.append(year)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT id, category, subcategory, name, value, unit, year, month, region, source, source_url, updated_at "
        f"FROM metrics {where} ORDER BY year DESC, id DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    conn.close()
    cols = ["id", "category", "subcategory", "name", "value", "unit", "year", "month", "region", "source", "source_url", "updated_at"]
    return [dict(zip(cols, r)) for r in rows]


def sqlite_metrics_search(q: str, limit: int = 100) -> list:
    if not _SQLITE_DB.exists():
        return []
    conn = sqlite3.connect(str(_SQLITE_DB))
    rows = conn.execute(
        "SELECT id, category, subcategory, name, value, unit, year, month, region, source, source_url, updated_at "
        "FROM metrics WHERE name LIKE ? OR category LIKE ? OR subcategory LIKE ? "
        "ORDER BY year DESC, id DESC LIMIT ?",
        (f"%{q}%", f"%{q}%", f"%{q}%", limit),
    ).fetchall()
    conn.close()
    cols = ["id", "category", "subcategory", "name", "value", "unit", "year", "month", "region", "source", "source_url", "updated_at"]
    return [dict(zip(cols, r)) for r in rows]


def sqlite_metrics_categories() -> list:
    if not _SQLITE_DB.exists():
        return []
    conn = sqlite3.connect(str(_SQLITE_DB))
    rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM metrics GROUP BY category ORDER BY cnt DESC"
    ).fetchall()
    conn.close()
    return [{"category": r[0], "count": r[1]} for r in rows]
