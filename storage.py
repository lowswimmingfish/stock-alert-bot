#!/usr/bin/env python3
"""스냅샷 저장 백엔드.

DATABASE_URL 환경변수가 있으면 Postgres(JSONB), 없으면 로컬 JSON 파일.
인터페이스는 기존 snapshots.json과 동일한 dict (날짜 문자열 → 스냅샷).
"""

import json
import logging
import os

from config_loader import DATA_DIR

logger = logging.getLogger(__name__)

SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

_schema_ready = False


def _connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                snap_date  DATE PRIMARY KEY,
                data       JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
    conn.commit()
    _schema_ready = True


def load_snapshots() -> dict:
    """전체 스냅샷 로드: {"YYYY-MM-DD": {...}, ...}"""
    if not DATABASE_URL:
        if SNAPSHOTS_FILE.exists():
            with open(SNAPSHOTS_FILE) as f:
                return json.load(f)
        return {}
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT snap_date, data FROM snapshots ORDER BY snap_date")
            return {str(d): data for d, data in cur.fetchall()}
    finally:
        conn.close()


def save_snapshots(data: dict):
    """전체 스냅샷 저장 (upsert + 없어진 날짜 삭제 — 이상값 정리 반영)."""
    if not DATABASE_URL:
        with open(SNAPSHOTS_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            for d, snap in data.items():
                cur.execute(
                    """
                    INSERT INTO snapshots (snap_date, data, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (snap_date)
                    DO UPDATE SET data = EXCLUDED.data, updated_at = now()
                    WHERE snapshots.data IS DISTINCT FROM EXCLUDED.data
                    """,
                    (d, json.dumps(snap)),
                )
            if data:
                cur.execute(
                    "DELETE FROM snapshots WHERE snap_date != ALL(%s::date[])",
                    (list(data.keys()),),
                )
        conn.commit()
    finally:
        conn.close()
