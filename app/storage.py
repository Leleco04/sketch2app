"""Interface unificada de armazenamento (disco local + sincronização opcional Supabase)."""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, SecretStr

from app.llm import ParsedOutput
from app.models import (
    CachedMessage,
    CachedSession,
    CachedVersion,
    Message,
    ProjectSummary,
    SessionRecord,
    VersionInfo,
)
from app.sessions import SessionError, SessionNotFound, SessionStore

logger = logging.getLogger(__name__)
BUCKET_NAME = "sketches"


class SupabaseSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    url: str = ""
    service_key: SecretStr = SecretStr("")

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip() and self.service_key.get_secret_value().strip())

    @classmethod
    def from_env(cls) -> "SupabaseSettings":
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        return cls(
            url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
            service_key=SecretStr(os.getenv("SUPABASE_SERVICE_KEY", "").strip()),
        )


def build_project_title(prompt: str, summary: str) -> str:
    source = prompt.strip() or summary.strip() or "Projeto sem título"
    clean = " ".join(source.split())
    return clean[:72] + ("…" if len(clean) > 72 else "")


def _record_to_summary(record: SessionRecord, *, synced_cloud: bool = False) -> ProjectSummary:
    latest_summary = record.versions[-1].summary if record.versions else ""
    first_summary = record.versions[0].summary if record.versions else ""
    return ProjectSummary(
        sid=record.sid,
        title=build_project_title(record.prompt, first_summary),
        prompt=record.prompt,
        summary=latest_summary,
        current=record.current,
        versions_count=len(record.versions),
        versions=[v.n for v in record.versions],
        has_image=record.prepared_image is not None,
        created_at=record.created_at,
        synced_cloud=synced_cloud,
    )


class DiskStorage:
    """Implementação padrão em disco local (local-first)."""

    backend_name = "disk"

    def __init__(self, store: SessionStore):
        self.store = store

    def save_session(self, sid: str) -> SessionRecord:
        return self.store.load(sid)

    def load_session(self, sid: str) -> SessionRecord:
        return self.store.load(sid)

    def list_sessions(self) -> list[ProjectSummary]:
        if not self.store.root.is_dir():
            return []
        items: list[ProjectSummary] = []
        for entry in self.store.root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not (entry / "session.json").is_file():
                continue
            try:
                record = self.store.load(entry.name)
                items.append(_record_to_summary(record, synced_cloud=False))
            except SessionError:
                continue
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items


class SupabaseStorage(DiskStorage):
    """Sincronização opcional com Supabase (bucket privado + tabelas PostgREST), local-first."""

    backend_name = "supabase"

    def __init__(
        self,
        store: SessionStore,
        settings: SupabaseSettings,
        *,
        http_client: httpx.Client | None = None,
    ):
        super().__init__(store)
        self.settings = settings
        self._client = http_client
        self._bucket_checked = False

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        key = self.settings.service_key.get_secret_value()
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{self.settings.url}{path}"
        if self._client is not None:
            return self._client.request(method, url, **kwargs)
        with httpx.Client(timeout=10.0) as client:
            return client.request(method, url, **kwargs)

    def _ensure_bucket(self) -> None:
        if self._bucket_checked:
            return
        resp = self._request("GET", f"/storage/v1/bucket/{BUCKET_NAME}", headers=self._headers())
        if resp.status_code == 404 or (resp.status_code == 400 and "not found" in resp.text.lower()):
            self._request(
                "POST",
                "/storage/v1/bucket",
                headers=self._headers({"Content-Type": "application/json"}),
                json={
                    "id": BUCKET_NAME,
                    "name": BUCKET_NAME,
                    "public": False,
                    "file_size_limit": 10 * 1024 * 1024,
                    "allowed_mime_types": ["image/jpeg", "image/png", "image/webp", "application/json"],
                },
            )
        self._bucket_checked = True

    def _build_snapshot(self, record: SessionRecord) -> tuple[CachedSession, bytes | None]:
        prepared_bytes = self.store.read_prepared_image(record.sid)
        cached_versions = [
            CachedVersion(
                n=v.n,
                summary=v.summary,
                html=self.store.read_version(record.sid, v.n),
                ts=v.ts,
                analysis=v.analysis,
                usage=v.usage,
                stage_timing=v.stage_timing,
            )
            for v in record.versions
        ]
        cached_messages = [
            CachedMessage(role=m.role, text=m.text, ts=m.ts) for m in record.messages
        ]
        snapshot = CachedSession(
            name=record.sid,
            prompt=record.prompt,
            created_at=record.created_at,
            has_image=prepared_bytes is not None,
            prepared_image_b64=(
                base64.b64encode(prepared_bytes).decode("ascii")
                if prepared_bytes is not None
                else None
            ),
            current=record.current,
            versions=cached_versions,
            messages=cached_messages,
        )
        return snapshot, prepared_bytes

    def save_session(self, sid: str) -> SessionRecord:
        record = super().save_session(sid)
        try:
            self._ensure_bucket()
            snapshot, prepared_bytes = self._build_snapshot(record)
            image_path: str | None = None
            if prepared_bytes is not None:
                image_path = f"{record.sid}/image.jpg"
                self._request(
                    "POST",
                    f"/storage/v1/object/{BUCKET_NAME}/{image_path}",
                    headers=self._headers({"Content-Type": "image/jpeg", "x-upsert": "true"}),
                    content=prepared_bytes,
                )
            snapshot_bytes = json.dumps(
                snapshot.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
            ).encode("utf-8")
            self._request(
                "POST",
                f"/storage/v1/object/{BUCKET_NAME}/{record.sid}/session.json",
                headers=self._headers({"Content-Type": "application/json", "x-upsert": "true"}),
                content=snapshot_bytes,
            )
            self._sync_tables(record, snapshot, image_path)
        except Exception:
            logger.warning("supabase sync skipped or failed for sid=%s (local session preserved)", sid)
        return record

    def _sync_tables(
        self, record: SessionRecord, snapshot: CachedSession, image_path: str | None
    ) -> None:
        first_summary = record.versions[0].summary if record.versions else ""
        upsert_headers = self._headers({
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        })
        proj_resp = self._request(
            "POST",
            "/rest/v1/projects",
            headers=upsert_headers,
            json={
                "sid": record.sid,
                "title": build_project_title(record.prompt, first_summary),
                "prompt": record.prompt,
                "image_path": image_path,
                "current_version": record.current,
                "created_at": record.created_at.isoformat(),
            },
        )
        if proj_resp.status_code >= 400:
            return
        version_rows = []
        for v in snapshot.versions:
            last_usage = v.usage[-1] if v.usage else None
            version_rows.append({
                "project_sid": record.sid,
                "n": v.n,
                "html": v.html,
                "summary": v.summary,
                "model": last_usage.model if last_usage else None,
                "tokens_in": last_usage.tokens_in if last_usage else None,
                "tokens_out": last_usage.tokens_out if last_usage else None,
                "latency_ms": last_usage.latency_ms if last_usage else None,
                "created_at": (v.ts or record.created_at).isoformat(),
            })
        if version_rows:
            self._request("POST", "/rest/v1/versions", headers=upsert_headers, json=version_rows)
        message_rows = [
            {
                "project_sid": record.sid,
                "seq": idx + 1,
                "role": m.role,
                "text": m.text,
                "created_at": (m.ts or record.created_at).isoformat(),
            }
            for idx, m in enumerate(snapshot.messages)
        ]
        if message_rows:
            self._request("POST", "/rest/v1/messages", headers=upsert_headers, json=message_rows)

    def _list_cloud_sids(self) -> set[str]:
        cloud_sids: set[str] = set()
        try:
            resp = self._request(
                "POST",
                f"/storage/v1/object/list/{BUCKET_NAME}",
                headers=self._headers({"Content-Type": "application/json"}),
                json={"prefix": "", "limit": 100, "offset": 0},
            )
            if resp.status_code == 200 and isinstance(resp.json(), list):
                for entry in resp.json():
                    name = entry.get("name", "") if isinstance(entry, dict) else ""
                    if len(name) == 32 and all(c in "0123456789abcdef" for c in name):
                        cloud_sids.add(name)
        except Exception:
            pass
        return cloud_sids

    def _download_cloud_session(self, sid: str) -> SessionRecord | None:
        try:
            resp = self._request(
                "GET",
                f"/storage/v1/object/{BUCKET_NAME}/{sid}/session.json",
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return None
            cached = CachedSession.model_validate(resp.json())
            prepared_bytes: bytes | None = None
            if cached.prepared_image_b64:
                prepared_bytes = base64.b64decode(cached.prepared_image_b64)
            else:
                img_resp = self._request(
                    "GET",
                    f"/storage/v1/object/{BUCKET_NAME}/{sid}/image.jpg",
                    headers=self._headers(),
                )
                if img_resp.status_code == 200 and img_resp.content:
                    prepared_bytes = img_resp.content
            return self._materialize_cached(sid, cached, prepared_bytes)
        except Exception:
            return None

    def _materialize_cached(
        self, sid: str, cached: CachedSession, prepared_bytes: bytes | None
    ) -> SessionRecord:
        session_dir = self.store.root / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        if prepared_bytes is not None:
            (session_dir / "original.bin").write_bytes(prepared_bytes)
            (session_dir / "image.jpg").write_bytes(prepared_bytes)
        version_infos: list[VersionInfo] = []
        now_utc = datetime.now(timezone.utc)
        for v in cached.versions:
            (session_dir / f"v{v.n}.html").write_text(v.html, encoding="utf-8")
            version_infos.append(
                VersionInfo(
                    n=v.n,
                    summary=v.summary,
                    ts=v.ts or cached.created_at or now_utc,
                    analysis=v.analysis,
                    usage=v.usage,
                    stage_timing=v.stage_timing,
                )
            )
        messages = [
            Message(role=m.role, text=m.text, ts=m.ts or cached.created_at or now_utc)
            for m in cached.messages
        ]
        record = SessionRecord(
            sid=sid,
            prompt=cached.prompt,
            created_at=cached.created_at or now_utc,
            original_image="original.bin" if prepared_bytes is not None else None,
            prepared_image="image.jpg" if prepared_bytes is not None else None,
            current=cached.current,
            versions=version_infos,
            messages=messages,
        )
        (session_dir / "session.json").write_text(
            json.dumps(record.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return record

    def load_session(self, sid: str) -> SessionRecord:
        try:
            return super().load_session(sid)
        except SessionNotFound:
            downloaded = self._download_cloud_session(sid)
            if downloaded is not None:
                return downloaded
            raise

    def list_sessions(self) -> list[ProjectSummary]:
        local_items = super().list_sessions()
        cloud_sids = self._list_cloud_sids()
        by_sid: dict[str, ProjectSummary] = {}
        for item in local_items:
            item.synced_cloud = item.sid in cloud_sids
            by_sid[item.sid] = item
        for sid in cloud_sids:
            if sid not in by_sid:
                downloaded = self._download_cloud_session(sid)
                if downloaded is not None:
                    by_sid[sid] = _record_to_summary(downloaded, synced_cloud=True)
        merged = list(by_sid.values())
        merged.sort(key=lambda item: item.created_at, reverse=True)
        return merged


def get_storage(store: SessionStore) -> DiskStorage:
    settings = SupabaseSettings.from_env()
    offline = os.getenv("OFFLINE_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
    if settings.enabled and not offline:
        return SupabaseStorage(store, settings)
    return DiskStorage(store)
