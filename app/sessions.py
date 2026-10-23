"""Sessões em disco; HTML separado dos metadados e publicação atômica da v1."""

import base64
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.llm import ParsedOutput
from app.models import (
    CACHE_NAME_PATTERN,
    SID_PATTERN,
    AttemptUsage,
    CachedMessage,
    CachedSession,
    CachedVersion,
    CacheEntryInfo,
    Message,
    SessionRecord,
    StageTiming,
    VersionInfo,
)


class SessionError(RuntimeError):
    """Erro de persistência sem expor caminhos ou conteúdo da sessão."""


class SessionNotFound(SessionError):
    """Sessão ou versão inexistente, ou identificador fora do contrato."""


class SessionStore:
    def __init__(self, root: Path, cache_root: Path | None = None):
        self.root = Path(root).resolve()
        self.cache_root = Path(cache_root).resolve() if cache_root is not None else (self.root.parent / "cache").resolve()

    def _directory(self, sid: str) -> Path:
        if not isinstance(sid, str) or not re.fullmatch(SID_PATTERN, sid):
            raise SessionNotFound("Sessão não encontrada.")
        path = (self.root / sid).resolve()
        if path.parent != self.root:
            raise SessionNotFound("Sessão não encontrada.")
        return path

    def _file(self, directory: Path, name: str) -> Path:
        path = (directory / name).resolve()
        if path.parent != directory:
            raise SessionNotFound("Versão não encontrada.")
        return path

    def save_initial(
        self, *, prompt: str, result: ParsedOutput,
        original_image: bytes | None = None, prepared_image: bytes | None = None,
        usage: list[AttemptUsage | dict] | None = None,
        stage_timing: list[StageTiming | dict] | None = None,
        replay_source: str | None = None,
    ) -> SessionRecord:
        if (original_image is None) != (prepared_image is None):
            raise SessionError("Não foi possível salvar as imagens da sessão.")
        started = time.monotonic()
        sid = uuid4().hex
        now = datetime.now(timezone.utc)
        try:
            version_info = VersionInfo(
                n=1,
                summary=result.summary,
                ts=now,
                analysis=list(result.analysis),
                usage=list(usage) if usage is not None else None,
                stage_timing=list(stage_timing) if stage_timing is not None else None,
            )
            record = SessionRecord(
                sid=sid, prompt=prompt, created_at=now, current=1,
                original_image="original.bin" if original_image is not None else None,
                prepared_image="image.jpg" if prepared_image is not None else None,
                replay_source=replay_source,
                versions=[version_info],
                messages=[
                    Message(role="user", text=prompt or "Desenho enviado.", ts=now),
                    Message(role="assistant", text=result.summary, ts=now),
                ],
            )
        except ValidationError:
            raise SessionError("Metadados da sessão inválidos.") from None
        destination = self._directory(sid)
        staging = (self.root / f".pending-{sid}").resolve()
        if staging.parent != self.root:
            raise SessionError("Não foi possível preparar a sessão.")
        created = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            staging.mkdir()
            created = True
            if original_image is not None:
                (staging / "original.bin").write_bytes(original_image)
                (staging / "image.jpg").write_bytes(prepared_image)
            (staging / "v1.html").write_text(result.html, encoding="utf-8")
            if version_info.stage_timing is not None and not any(
                item.step == "gravando" for item in version_info.stage_timing
            ):
                version_info.stage_timing.append(
                    StageTiming(step="gravando", ms=max(0, round((time.monotonic() - started) * 1000)))
                )
            (staging / "session.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
            if destination.exists():
                raise OSError("Identificador já existente.")
            staging.rename(destination)
        except (OSError, UnicodeError):
            if created:
                # Só arquivos conhecidos da pasta recém-criada; sem remoção recursiva.
                for name in ("original.bin", "image.jpg", "v1.html", "session.json"):
                    try:
                        (staging / name).unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    staging.rmdir()
                except OSError:
                    pass
            raise SessionError("Não foi possível gravar a sessão no disco.") from None
        return record

    def load(self, sid: str) -> SessionRecord:
        path = self._file(self._directory(sid), "session.json")
        try:
            record = SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.sid != sid:
                raise SessionError("Metadados da sessão inconsistentes.")
            return record
        except FileNotFoundError:
            raise SessionNotFound("Sessão não encontrada.") from None
        except (OSError, UnicodeError, ValidationError):
            raise SessionError("Não foi possível ler os metadados da sessão.") from None

    def read_version(self, sid: str, version: int) -> str:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SessionNotFound("Versão não encontrada.")
        record = self.load(sid)
        if version not in {item.n for item in record.versions}:
            raise SessionNotFound("Versão não encontrada.")
        path = self._file(self._directory(sid), f"v{version}.html")
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SessionNotFound("Versão não encontrada.") from None
        except (OSError, UnicodeError):
            raise SessionError("Não foi possível ler o HTML da versão.") from None

    def read_prepared_image(self, sid: str) -> bytes | None:
        record = self.load(sid)
        if record.prepared_image is None:
            return None
        path = self._file(self._directory(sid), record.prepared_image)
        try:
            data = path.read_bytes()
        except OSError:
            raise SessionError("Não foi possível ler a imagem da sessão.") from None
        if not data:
            raise SessionError("Não foi possível ler a imagem da sessão.")
        return data

    def save_version(
        self,
        sid: str,
        *,
        prompt: str,
        result: ParsedOutput,
        usage: list[AttemptUsage | dict] | None = None,
        stage_timing: list[StageTiming | dict] | None = None,
    ) -> SessionRecord:
        started = time.monotonic()
        clean_prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not clean_prompt:
            raise SessionError("A mensagem do chat não pode estar vazia.")
        record = self.load(sid)
        directory = self._directory(sid)
        next_n = max(item.n for item in record.versions) + 1
        now = datetime.now(timezone.utc)
        try:
            version_info = VersionInfo(
                n=next_n,
                summary=result.summary,
                ts=now,
                analysis=list(result.analysis),
                usage=list(usage) if usage is not None else None,
                stage_timing=list(stage_timing) if stage_timing is not None else None,
            )
            updated = SessionRecord(
                sid=record.sid,
                prompt=record.prompt,
                created_at=record.created_at,
                original_image=record.original_image,
                prepared_image=record.prepared_image,
                replay_source=record.replay_source,
                current=next_n,
                versions=[*record.versions, version_info],
                messages=[
                    *record.messages,
                    Message(role="user", text=clean_prompt, ts=now),
                    Message(role="assistant", text=result.summary, ts=now),
                ],
            )
        except ValidationError:
            raise SessionError("Metadados da sessão inválidos.") from None
        html_target = self._file(directory, f"v{next_n}.html")
        html_staging = self._file(directory, f".pending-v{next_n}.html")
        json_target = self._file(directory, "session.json")
        json_staging = self._file(directory, ".pending-session.json")
        html_published = False
        try:
            if html_target.exists():
                raise OSError("Versão já existente.")
            html_staging.write_text(result.html, encoding="utf-8")
            html_staging.replace(html_target)
            html_published = True
            if version_info.stage_timing is not None and not any(
                item.step == "gravando" for item in version_info.stage_timing
            ):
                version_info.stage_timing.append(
                    StageTiming(step="gravando", ms=max(0, round((time.monotonic() - started) * 1000)))
                )
            json_staging.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
            json_staging.replace(json_target)
        except (OSError, UnicodeError):
            for candidate in (html_staging, json_staging):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
            if html_published:
                try:
                    html_target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise SessionError("Não foi possível gravar a nova versão no disco.") from None
        return updated

    def restore_version(self, sid: str, version: int) -> SessionRecord:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise SessionNotFound("Versão não encontrada.")
        record = self.load(sid)
        if version not in {item.n for item in record.versions}:
            raise SessionNotFound("Versão não encontrada.")
        # Garante que o arquivo HTML da versão existe e está íntegro antes de alternar.
        self.read_version(sid, version)
        if record.current == version:
            return record
        try:
            updated = record.model_copy(update={"current": version})
            SessionRecord.model_validate(updated.model_dump())
        except ValidationError:
            raise SessionError("Metadados da sessão inválidos.") from None
        directory = self._directory(sid)
        json_target = self._file(directory, "session.json")
        json_staging = self._file(directory, ".pending-session.json")
        try:
            json_staging.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
            json_staging.replace(json_target)
        except (OSError, UnicodeError):
            try:
                json_staging.unlink(missing_ok=True)
            except OSError:
                pass
            raise SessionError("Não foi possível atualizar a versão ativa da sessão.") from None
        return updated

    def _normalize_cache_name(self, name: str) -> str:
        if not isinstance(name, str):
            raise SessionNotFound("Gravação não encontrada no cache.")
        clean = name.strip()
        if clean.lower().endswith(".json"):
            clean = clean[:-5]
        if not re.fullmatch(CACHE_NAME_PATTERN, clean):
            raise SessionNotFound("Gravação não encontrada no cache.")
        return clean

    def _cache_file(self, name: str) -> tuple[str, Path]:
        clean = self._normalize_cache_name(name)
        path = (self.cache_root / f"{clean}.json").resolve()
        if path.parent != self.cache_root:
            raise SessionNotFound("Gravação não encontrada no cache.")
        return clean, path

    def export_to_cache(self, sid: str, name: str | None = None) -> Path:
        record = self.load(sid)
        clean, target = self._cache_file(name or record.sid)
        directory = self._directory(sid)
        original_b64 = prepared_b64 = None
        try:
            if record.original_image is not None:
                original_bytes = self._file(directory, record.original_image).read_bytes()
                original_b64 = base64.b64encode(original_bytes).decode("ascii")
            if record.prepared_image is not None:
                prepared_bytes = self._file(directory, record.prepared_image).read_bytes()
                prepared_b64 = base64.b64encode(prepared_bytes).decode("ascii")
            cached_versions = [
                CachedVersion(
                    n=v.n,
                    summary=v.summary,
                    html=self.read_version(sid, v.n),
                    ts=v.ts,
                    analysis=v.analysis,
                    usage=v.usage,
                    stage_timing=v.stage_timing,
                )
                for v in record.versions
            ]
            cached = CachedSession(
                name=clean,
                prompt=record.prompt,
                created_at=record.created_at,
                has_image=record.prepared_image is not None,
                original_image_b64=original_b64,
                prepared_image_b64=prepared_b64,
                current=record.current,
                versions=cached_versions,
                messages=[CachedMessage(role=m.role, text=m.text, ts=m.ts) for m in record.messages],
            )
        except ValidationError:
            raise SessionError("Metadados da gravação em cache são inválidos.") from None
        except OSError:
            raise SessionError("Não foi possível ler os arquivos da sessão para gravação em cache.") from None

        staging = (self.cache_root / f".pending-{clean}.json").resolve()
        if staging.parent != self.cache_root:
            raise SessionError("Não foi possível preparar o arquivo de cache.")
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            staging.write_text(cached.model_dump_json(indent=2), encoding="utf-8")
            staging.replace(target)
        except (OSError, UnicodeError):
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            raise SessionError("Não foi possível salvar a gravação em cache.") from None
        return target

    def load_cached(self, name: str) -> tuple[str, CachedSession]:
        clean, path = self._cache_file(name)
        try:
            cached = CachedSession.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionNotFound("Gravação não encontrada no cache.") from None
        except (OSError, UnicodeError, ValidationError):
            raise SessionError("Não foi possível ler a gravação do cache.") from None
        return clean, cached

    def list_cache(self) -> list[CacheEntryInfo]:
        if not self.cache_root.exists():
            return []
        try:
            candidates = sorted(
                (p for p in self.cache_root.glob("*.json") if not p.name.startswith(".")),
                key=lambda p: (-p.stat().st_mtime, p.name),
            )
        except OSError:
            raise SessionError("Não foi possível listar as gravações do cache.") from None
        items: list[CacheEntryInfo] = []
        for path in candidates:
            stem = path.stem
            if not re.fullmatch(CACHE_NAME_PATTERN, stem):
                continue
            try:
                cached = CachedSession.model_validate_json(path.read_text(encoding="utf-8"))
                first = next((v for v in cached.versions if v.n == 1), cached.versions[0])
                has_metrics = any(bool(v.usage) for v in cached.versions)
                items.append(
                    CacheEntryInfo(
                        name=stem,
                        prompt=cached.prompt,
                        summary=first.summary,
                        versions=len(cached.versions),
                        has_image=bool(
                            cached.has_image
                            or cached.prepared_image_b64
                            or cached.original_image_b64
                        ),
                        has_metrics=has_metrics,
                    )
                )
            except (OSError, UnicodeError, ValidationError):
                continue
        return items
