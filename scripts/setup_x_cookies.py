"""X (Twitter) Cookie を .env に設定し、last30days の X 検索を有効化する。

使い方:
  1. Chrome または Edge で https://x.com にログイン
  2. F12 → Application → Cookies → https://x.com
  3. auth_token と ct0 の Value をコピー
  4. python scripts/setup_x_cookies.py

Chrome を閉じていれば自動読み取りも試みます（EBUSY のときは手動入力）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
LAST30_ENV = Path.home() / ".config" / "last30days" / ".env"
BIRD_DIR = Path.home() / ".claude" / "skills" / "last30days" / "scripts" / "lib" / "vendor" / "bird-search"


def _try_auto_from_browser() -> tuple[str, str] | None:
    """bird-search --check でブラウザ Cookie 自動検出を試す。"""
    bird = BIRD_DIR / "bird-search.mjs"
    if not bird.is_file():
        print("bird-search が見つかりません。手動入力に進みます。")
        return None
    try:
        r = subprocess.run(
            ["node", str(bird), "--check"],
            cwd=str(BIRD_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads((r.stdout or "").strip() or "{}")
        if data.get("authenticated"):
            print(f"ブラウザから Cookie 検出 OK (source={data.get('source')})")
            # bird は Cookie 値を stdout に出さないので env 経由で再取得不可 → 手動へ
    except Exception as e:
        print(f"自動検出スキップ: {e}")
    return None


def _read_existing_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _verify(auth_token: str, ct0: str) -> bool:
    bird = BIRD_DIR / "bird-search.mjs"
    if not bird.is_file():
        return False
    env = os.environ.copy()
    env["AUTH_TOKEN"] = auth_token
    env["CT0"] = ct0
    try:
        r = subprocess.run(
            ["node", str(bird), "--check"],
            cwd=str(BIRD_DIR),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        data = json.loads((r.stdout or "").strip() or "{}")
        if data.get("authenticated"):
            print(f"✓ X 認証 OK (source={data.get('source')})")
            return True
        print(f"✗ 認証失敗: {data}")
    except Exception as e:
        print(f"✗ 検証エラー: {e}")
    return False


def main() -> int:
    print("=" * 60)
    print("X (Twitter) Cookie 設定 — last30days バズ収集用（無料）")
    print("=" * 60)
    print()
    print("【Cookie の取り方】")
    print("  1. Chrome/Edge で https://x.com にログイン")
    print("  2. F12 → Application → Storage → Cookies → https://x.com")
    print("  3. 次の2つをコピー:")
    print("     - auth_token  → AUTH_TOKEN")
    print("     - ct0         → CT0")
    print()
    print("※ Chrome が Cookie ファイルをロックしている場合は")
    print("  ブラウザを一度閉じると自動検出できることもあります。")
    print()

    _try_auto_from_browser()

    existing = _read_existing_env(ENV_FILE)
    auth_default = existing.get("AUTH_TOKEN", "")
    ct0_default = existing.get("CT0", "")

    auth = input(f"AUTH_TOKEN (auth_token の値) [{('設定済み' if auth_default else '未設定')}]: ").strip()
    if not auth:
        auth = auth_default
    ct0 = input(f"CT0 (ct0 の値) [{('設定済み' if ct0_default else '未設定')}]: ").strip()
    if not ct0:
        ct0 = ct0_default

    if not auth or not ct0:
        print("\nAUTH_TOKEN と CT0 の両方が必要です。中断します。")
        return 1

    if len(auth) < 20 or len(ct0) < 20:
        print("\n値が短すぎるようです。Cookie を正しくコピーしたか確認してください。")
        return 1

    updates = {"AUTH_TOKEN": auth, "CT0": ct0}
    _upsert_env(ENV_FILE, updates)
    _upsert_env(LAST30_ENV, updates)
    print(f"\n保存しました:")
    print(f"  - {ENV_FILE}")
    print(f"  - {LAST30_ENV}")

    if not _verify(auth, ct0):
        print("\n認証検証に失敗しました。Cookie の有効期限切れの可能性があります。")
        print("x.com に再ログインしてから再度実行してください。")
        return 1

    print("\n完了。python main.py を再起動すると Claude リサーチで X トレンドが使えます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
