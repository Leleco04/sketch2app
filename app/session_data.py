"""Armazenamento SQLite por sessão para a API de dados (/api/data/{sid}/{colecao})."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
from uuid import uuid4

_SID_RE = re.compile(r"^[0-9a-f]{32}$")
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

MAX_ITEM_BYTES = 16 * 1024
MAX_ITEMS_PER_SESSION = 200


class SessionDataError(ValueError):
    def __init__(self, code: str, msg: str, status_code: int = 400) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.status_code = status_code


class SessionDataStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.row_factory = sqlite3.Row
            if not self._initialized:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS session_items (
                            sid TEXT NOT NULL,
                            collection TEXT NOT NULL,
                            item_id TEXT NOT NULL,
                            data_json TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY (sid, collection, item_id)
                        )
                        """
                    )
                self._initialized = True
            yield conn

    @staticmethod
    def _validate_keys(sid: str, collection: str, item_id: str | None = None) -> tuple[str, str, str | None]:
        if not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
            raise SessionDataError("invalid_sid", "Identificador de sessão inválido.", 422)
        if not isinstance(collection, str) or not _COLLECTION_RE.fullmatch(collection):
            raise SessionDataError("invalid_collection", "Nome de coleção inválido.", 422)
        if item_id is not None:
            if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
                raise SessionDataError("invalid_item_id", "Identificador de item inválido.", 422)
        return sid, collection, item_id

    @staticmethod
    def _normalize_payload(payload: Any, explicit_id: str | None = None) -> tuple[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise SessionDataError("invalid_payload", "O corpo deve ser um objeto JSON.", 422)
        data = dict(payload)
        raw_id = explicit_id or data.get("id")
        if raw_id is not None:
            item_id = str(raw_id).strip()
            if not _ITEM_ID_RE.fullmatch(item_id):
                raise SessionDataError("invalid_item_id", "Identificador de item inválido.", 422)
        else:
            item_id = uuid4().hex[:12]
        data["id"] = item_id
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_ITEM_BYTES:
            raise SessionDataError("payload_too_large", "O item excede o limite de 16 KiB.", 413)
        return item_id, data

    def list_items(self, sid: str, collection: str) -> list[dict[str, Any]]:
        sid, collection, _ = self._validate_keys(sid, collection)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json FROM session_items
                WHERE sid = ? AND collection = ?
                ORDER BY updated_at ASC, rowid ASC
                """,
                (sid, collection),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed = json.loads(row["data_json"])
                if isinstance(parsed, dict):
                    result.append(parsed)
            except ValueError:
                continue
        return result

    def create_item(self, sid: str, collection: str, payload: Any) -> dict[str, Any]:
        sid, collection, _ = self._validate_keys(sid, collection)
        item_id, data = self._normalize_payload(payload)
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(data, ensure_ascii=False)
        with self._lock, self._connect() as conn, conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM session_items WHERE sid = ?",
                (sid,),
            ).fetchone()
            if count_row and int(count_row["cnt"]) >= MAX_ITEMS_PER_SESSION:
                raise SessionDataError(
                    "quota_exceeded",
                    "Limite de itens da sessão atingido.",
                    429,
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO session_items (sid, collection, item_id, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, collection, item_id, encoded, now),
            )
        return data

    def upsert_item(
        self,
        sid: str,
        collection: str,
        payload: Any,
        *,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        sid, collection, validated_id = self._validate_keys(sid, collection, item_id)
        resolved_id, data = self._normalize_payload(payload, explicit_id=validated_id)
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(data, ensure_ascii=False)
        with self._lock, self._connect() as conn, conn:
            existing = conn.execute(
                "SELECT 1 FROM session_items WHERE sid = ? AND collection = ? AND item_id = ?",
                (sid, collection, resolved_id),
            ).fetchone()
            if not existing:
                count_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM session_items WHERE sid = ?",
                    (sid,),
                ).fetchone()
                if count_row and int(count_row["cnt"]) >= MAX_ITEMS_PER_SESSION:
                    raise SessionDataError(
                        "quota_exceeded",
                        "Limite de itens da sessão atingido.",
                        429,
                    )
            conn.execute(
                """
                INSERT OR REPLACE INTO session_items (sid, collection, item_id, data_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, collection, resolved_id, encoded, now),
            )
        return data

    def delete_items(
        self,
        sid: str,
        collection: str,
        *,
        item_id: str | None = None,
    ) -> int:
        sid, collection, validated_id = self._validate_keys(sid, collection, item_id)
        with self._lock, self._connect() as conn, conn:
            if validated_id is not None:
                cur = conn.execute(
                    "DELETE FROM session_items WHERE sid = ? AND collection = ? AND item_id = ?",
                    (sid, collection, validated_id),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM session_items WHERE sid = ? AND collection = ?",
                    (sid, collection),
                )
            return int(cur.rowcount or 0)
