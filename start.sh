#!/bin/bash
# Render は PORT を設定する。未設定時は 10000（Render のデフォルト）で待ち受け
python scripts/generate_sitemap.py || true
uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
