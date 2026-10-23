"""Ponto de entrada do servidor local."""

import asyncio
import base64
import json
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Annotated

from anyio import CancelScope
from dotenv import load_dotenv
from fastapi import FastAPI, Path as PathParameter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import (
    audience as audience_mod,
    imaging,
    llm,
    prompts,
    session_data as session_data_mod,
    storage as storage_mod,
)
from app.models import (
    AttemptUsage,
    AudienceQueueResponse,
    CachedSession,
    CachedVersion,
    CacheListResponse,
    ChatMessageRequest,
    CreateSessionRequest,
    ErrorResponse,
    FrontendConfig,
    GenerationResult,
    HubListResponse,
    PromptsResponse,
    QueueItem,
    RestoreVersionRequest,
    SessionDetailResponse,
    StageTiming,
    UploadDrawingRequest,
)
from app.sessions import SessionError, SessionNotFound, SessionStore

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

app = FastAPI(title="Sketch2App")
store = SessionStore(BASE_DIR / "generated")
audience_queue = audience_mod.AudienceQueue(BASE_DIR / "generated" / "queue")
data_store = session_data_mod.SessionDataStore(BASE_DIR / "generated" / "session_data.sqlite3")
_generation_lock = asyncio.Lock()

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver", ""})
_TUNNEL_HEADERS = frozenset({
    b"cf-connecting-ip",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-original-host",
    b"forwarded",
})
_TUNNEL_APP_RE = re.compile(r"^/app/[0-9a-f]{32}/[1-9][0-9]*$")
_TUNNEL_DATA_RE = re.compile(r"^/api/data/[0-9a-f]{32}/[A-Za-z0-9_-]{1,40}(?:/[A-Za-z0-9_-]{1,64})?$")


def _is_tunnel_scope(scope: dict) -> bool:
    headers = scope.get("headers") or ()
    host_val = ""
    for k, v in headers:
        kl = k.lower()
        if kl in _TUNNEL_HEADERS:
            return True
        if kl == b"host":
            host_val = v.decode("latin1", errors="ignore").strip().lower()
    if host_val:
        if host_val.startswith("["):
            end = host_val.find("]")
            hostname = host_val[: end + 1] if end != -1 else host_val
        else:
            hostname = host_val.split(":", 1)[0]
        if hostname not in _LOCAL_HOSTS:
            return True
    return False


def _is_allowed_tunnel_request(method: str, path: str) -> bool:
    if method == "GET" and (path in {"/enviar", "/style.css", "/favicon.svg"} or path.startswith("/assets/")):
        return True
    if method == "POST" and path == "/upload":
        return True
    if method == "GET" and _TUNNEL_APP_RE.fullmatch(path):
        return True
    if method == "GET" and path.startswith("/vendor/"):
        return True
    if method in {"GET", "POST", "PUT", "DELETE", "OPTIONS"} and _TUNNEL_DATA_RE.fullmatch(path):
        return True
    return False


class _TunnelGuardMiddleware:
    def __init__(self, inner_app):
        self.app = inner_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _is_tunnel_scope(scope):
            method = (scope.get("method") or "GET").upper()
            path = scope.get("path") or "/"
            if not _is_allowed_tunnel_request(method, path):
                resp = _error(403, "forbidden", "Rota restrita ao operador local.")
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(_TunnelGuardMiddleware)


def _error(status: int, code: str, msg: str, hint: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(code=code, msg=msg, hint=hint).model_dump())


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"


class _GenerationStream(StreamingResponse):
    async def stream_response(self, send):
        try:
            await super().stream_response(send)
        finally:
            # ASGI 2.4 signals disconnect by failing send(), outside the iterator.
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def _stream(events, status: int = 200) -> StreamingResponse:
    return _GenerationStream(events, status_code=status, media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    # Não devolver os valores de entrada presentes nos erros de validação.
    too_large = any(item["type"] == "string_too_long" and "image_b64" in item["loc"] for item in error.errors())
    status, code, msg = (413, "image_too_large", "A imagem deve ter no máximo 10 MiB.") if too_large else (
        422, "invalid_request", "Requisição inválida. Confira os campos e informe imagem e/ou texto válidos."
    )
    if request.method == "POST" and (
        request.url.path == "/api/sessions"
        or (request.url.path.startswith("/api/sessions/") and request.url.path.endswith("/messages"))
    ):
        return _stream(iter([_event("error", ErrorResponse(code=code, msg=msg).model_dump())]), status)
    return _error(status, code, msg)


def _run_generation_stream(produce) -> StreamingResponse:
    async def events():
        if _generation_lock.locked():
            yield _event(
                "error",
                ErrorResponse(
                    code="busy",
                    msg="Já existe uma geração em andamento. Aguarde sua conclusão.",
                ).model_dump(),
            )
            return
        async with _generation_lock:
            queue = asyncio.Queue(maxsize=16)

            async def emit(name, data):
                await queue.put((name, data))

            worker = asyncio.create_task(produce(emit))
            try:
                while True:
                    try:
                        name, data = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _event(name, data)
                    if name in {"done", "error"}:
                        break
            finally:
                # Starlette cancela o grupo ASGI 2.3 repetidamente durante a
                # desconexão. Proteger o cleanup mantém o lock até o disco terminar.
                with CancelScope(shield=True):
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker

    return _stream(events())


def _maybe_record_to_cache(sid: str) -> None:
    try:
        settings = llm.LLMSettings.from_env()
        if settings.record:
            store.export_to_cache(sid)
    except llm.LLMError:
        pass
    with suppress(Exception):
        backend = storage_mod.get_storage(store)
        if backend.backend_name == "supabase":
            backend.save_session(sid)


def _select_replay_chat_version(
    cached: CachedSession, next_n: int, text: str, offline: bool
) -> CachedVersion | None:
    chat_versions = [v for v in sorted(cached.versions, key=lambda item: item.n) if v.n >= 2]
    if not chat_versions:
        return cached.versions[-1] if offline else None
    needle = text.strip().lower()
    # Associa cada versão gravada v2..vK ao pedido do usuário correspondente em messages.
    user_messages = [m.text.strip().lower() for m in cached.messages if m.role == "user"]
    for idx, candidate in enumerate(chat_versions):
        msg_idx = idx + 1
        if msg_idx < len(user_messages) and user_messages[msg_idx] == needle and candidate.n >= next_n:
            return candidate
    by_number = next((v for v in chat_versions if v.n == next_n), None)
    if by_number is not None:
        return by_number
    if offline:
        for idx, candidate in enumerate(chat_versions):
            msg_idx = idx + 1
            if msg_idx < len(user_messages) and user_messages[msg_idx] == needle:
                return candidate
        return chat_versions[-1]
    return None


@app.get("/api/config", response_model=FrontendConfig)
def frontend_config():
    try:
        settings = llm.LLMSettings.from_env()
    except llm.LLMError as error:
        return _error(500, error.code, error.msg, error.hint)
    return FrontendConfig(
        mode="offline" if settings.offline else "mock" if settings.mock else "live",
        max_image_bytes=imaging.MAX_IMAGE_BYTES,
    )


@app.post("/api/sessions", response_class=StreamingResponse)
async def create_session(payload: CreateSessionRequest):
    async def produce(emit):
        original = prepared = None
        usages: list[AttemptUsage] = []
        timings: list[StageTiming] = []

        async def record_and_emit(name: str, data: dict) -> None:
            if name == "usage":
                usages.append(AttemptUsage.model_validate(data))
            elif name == "stage_timing":
                timings.append(StageTiming.model_validate(data))
            await emit(name, data)

        try:
            prep_started = llm._now()
            await emit("status", {"step": "preparando", "msg": "Preparando seu desenho e suas instruções…"})
            if payload.image_b64 is not None:
                original = imaging.decode_image_b64(payload.image_b64)
                prepared = await run_in_threadpool(imaging.preprocess_image, original)
            prep_ms = max(0, round((llm._now() - prep_started) * 1000))
            await record_and_emit("stage_timing", {"step": "preparando", "ms": prep_ms})
            extras_kw = {"use_extras": True} if payload.use_extras else {}
            if payload.use_data_api:
                extras_kw["use_data_api"] = True
            result = await llm.generate_app(
                image=prepared, prompt=payload.prompt or "", on_event=record_and_emit, **extras_kw
            )
            save_started = llm._now()
            await emit("status", {"step": "gravando", "msg": "Salvando a sessão no disco…"})
            # Uma gravação iniciada termina antes de liberar o lock, inclusive
            # se o cliente desconectar: threads não podem ser interrompidas.
            saving = asyncio.create_task(run_in_threadpool(
                store.save_initial, prompt=payload.prompt or "", result=result,
                original_image=original, prepared_image=prepared,
                usage=usages, stage_timing=timings,
            ))
            try:
                record = await asyncio.shield(saving)
            except asyncio.CancelledError:
                with suppress(SessionError):
                    await saving
                raise
            await run_in_threadpool(_maybe_record_to_cache, record.sid)
            save_ms = (
                record.versions[0].stage_timing[-1].ms
                if record.versions[0].stage_timing and record.versions[0].stage_timing[-1].step == "gravando"
                else max(0, round((llm._now() - save_started) * 1000))
            )
            await emit("stage_timing", {"step": "gravando", "ms": save_ms})
            await emit("done", GenerationResult(sid=record.sid, version=1, url=f"/app/{record.sid}/1", summary=result.summary).model_dump())
        except imaging.ImageTooLargeError as error:
            await emit("error", ErrorResponse(code="image_too_large", msg=str(error)).model_dump())
        except imaging.ImageProcessingError as error:
            await emit("error", ErrorResponse(code="invalid_image", msg=str(error)).model_dump())
        except llm.LLMError as error:
            await emit("error", ErrorResponse(code=error.code, msg=error.msg, hint=error.hint).model_dump())
        except SessionError as error:
            await emit("error", ErrorResponse(code="storage_error", msg=str(error)).model_dump())
        except Exception:
            logging.getLogger(__name__).error("session generation failed unexpectedly")
            await emit("error", ErrorResponse(code="internal_error", msg="Não foi possível concluir a geração.").model_dump())

    return _run_generation_stream(produce)


@app.get("/api/sessions/{sid}", response_model=SessionDetailResponse)
def get_session(sid: str):
    try:
        record = storage_mod.get_storage(store).load_session(sid)
    except SessionNotFound as error:
        return _error(404, "not_found", str(error))
    except SessionError as error:
        return _error(500, "storage_error", str(error))
    return SessionDetailResponse(
        sid=record.sid,
        current=record.current,
        has_image=record.prepared_image is not None,
        versions=record.versions,
        messages=record.messages,
    )


@app.post("/api/sessions/{sid}/messages", response_class=StreamingResponse)
async def send_chat_message(sid: str, payload: ChatMessageRequest):
    async def produce(emit):
        usages: list[AttemptUsage] = []
        timings: list[StageTiming] = []

        async def record_and_emit(name: str, data: dict) -> None:
            if name == "usage":
                usages.append(AttemptUsage.model_validate(data))
            elif name == "stage_timing":
                timings.append(StageTiming.model_validate(data))
            await emit(name, data)

        try:
            prep_started = llm._now()
            await emit("status", {"step": "preparando", "msg": "Preparando o contexto da sessão e seu pedido…"})
            record = await run_in_threadpool(store.load, sid)
            current_html = await run_in_threadpool(store.read_version, sid, record.current)
            replay_version: CachedVersion | None = None
            if record.replay_source is not None:
                settings = llm.LLMSettings.from_env()
                _, cached = await run_in_threadpool(store.load_cached, record.replay_source)
                next_n = max(item.n for item in record.versions) + 1
                replay_version = _select_replay_chat_version(
                    cached, next_n, payload.text, settings.offline
                )
            prepared = (
                await run_in_threadpool(store.read_prepared_image, sid)
                if payload.reattach_image and replay_version is None
                else None
            )
            prep_ms = max(0, round((llm._now() - prep_started) * 1000))
            await record_and_emit("stage_timing", {"step": "preparando", "ms": prep_ms})
            if replay_version is not None:
                result = await llm.stream_recorded_version(
                    summary=replay_version.summary,
                    html=replay_version.html,
                    analysis=None,
                    usage=replay_version.usage,
                    stage_timing=replay_version.stage_timing,
                    is_initial=False,
                    on_event=record_and_emit,
                )
            else:
                extras_kw = {"use_extras": True} if payload.use_extras else {}
                if payload.use_data_api:
                    extras_kw["use_data_api"] = True
                result = await llm.generate_app(
                    image=prepared,
                    prompt=payload.text,
                    current_html=current_html,
                    history=record.messages,
                    on_event=record_and_emit,
                    **extras_kw,
                )
            save_started = llm._now()
            await emit("status", {"step": "gravando", "msg": "Salvando a nova versão no disco…"})
            saving = asyncio.create_task(run_in_threadpool(
                store.save_version,
                sid,
                prompt=payload.text,
                result=result,
                usage=usages if usages else None,
                stage_timing=timings,
            ))
            try:
                updated = await asyncio.shield(saving)
            except asyncio.CancelledError:
                with suppress(SessionError):
                    await saving
                raise
            if updated.replay_source is None:
                await run_in_threadpool(_maybe_record_to_cache, updated.sid)
            latest_version = updated.versions[-1]
            save_ms = (
                latest_version.stage_timing[-1].ms
                if latest_version.stage_timing and latest_version.stage_timing[-1].step == "gravando"
                else max(0, round((llm._now() - save_started) * 1000))
            )
            await emit("stage_timing", {"step": "gravando", "ms": save_ms})
            await emit(
                "done",
                GenerationResult(
                    sid=updated.sid,
                    version=updated.current,
                    url=f"/app/{updated.sid}/{updated.current}",
                    summary=result.summary,
                ).model_dump(),
            )
        except SessionNotFound as error:
            await emit("error", ErrorResponse(code="not_found", msg=str(error)).model_dump())
        except llm.LLMError as error:
            await emit("error", ErrorResponse(code=error.code, msg=error.msg, hint=error.hint).model_dump())
        except SessionError as error:
            await emit("error", ErrorResponse(code="storage_error", msg=str(error)).model_dump())
        except Exception:
            logging.getLogger(__name__).error("chat generation failed unexpectedly")
            await emit("error", ErrorResponse(code="internal_error", msg="Não foi possível concluir o ajuste.").model_dump())

    return _run_generation_stream(produce)


@app.post("/api/sessions/{sid}/restore", response_model=GenerationResult)
async def restore_session_version(sid: str, payload: RestoreVersionRequest):
    if _generation_lock.locked():
        return _error(409, "busy", "Já existe uma geração em andamento. Aguarde sua conclusão.")
    async with _generation_lock:
        try:
            def persist_restore():
                rec = store.restore_version(sid, payload.version)
                if rec.replay_source is None:
                    _maybe_record_to_cache(rec.sid)
                return rec

            record = await run_in_threadpool(persist_restore)
        except SessionNotFound as error:
            return _error(404, "not_found", str(error))
        except SessionError as error:
            return _error(500, "storage_error", str(error))
        target = next(item for item in record.versions if item.n == record.current)
        logging.getLogger(__name__).info(
            "session restore sid=%s version=%d api_calls=0", record.sid, record.current
        )
        return GenerationResult(
            sid=record.sid,
            version=record.current,
            url=f"/app/{record.sid}/{record.current}",
            summary=target.summary,
        )


@app.get("/api/cache", response_model=CacheListResponse)
def list_cached_sessions():
    try:
        return CacheListResponse(items=store.list_cache())
    except SessionError as error:
        return _error(500, "storage_error", str(error))


@app.post("/api/replay/{name}", response_class=StreamingResponse)
async def replay_cached_session(name: str):
    async def produce(emit):
        usages: list[AttemptUsage] = []
        timings: list[StageTiming] = []

        async def record_and_emit(evt_name: str, data: dict) -> None:
            if evt_name == "usage":
                usages.append(AttemptUsage.model_validate(data))
            elif evt_name == "stage_timing":
                timings.append(StageTiming.model_validate(data))
            await emit(evt_name, data)

        try:
            prep_started = llm._now()
            await emit("status", {"step": "preparando", "msg": "Preparando gravação offline do cache…"})
            clean_name, cached = await run_in_threadpool(store.load_cached, name)
            v1 = next((v for v in cached.versions if v.n == 1), cached.versions[0])
            original = prepared = None
            if cached.original_image_b64 or cached.prepared_image_b64:
                orig_b64 = cached.original_image_b64 or cached.prepared_image_b64
                prep_b64 = cached.prepared_image_b64 or cached.original_image_b64
                original = imaging.decode_image_b64(orig_b64)
                prepared = imaging.decode_image_b64(prep_b64)
            prep_ms = max(0, round((llm._now() - prep_started) * 1000))
            if v1.stage_timing:
                recorded_prep = next((s.ms for s in v1.stage_timing if s.step == "preparando"), None)
                if recorded_prep is not None:
                    prep_ms = recorded_prep
            await record_and_emit("stage_timing", {"step": "preparando", "ms": prep_ms})
            result = await llm.stream_recorded_version(
                summary=v1.summary,
                html=v1.html,
                analysis=v1.analysis,
                usage=v1.usage,
                stage_timing=v1.stage_timing,
                is_initial=True,
                on_event=record_and_emit,
            )
            save_started = llm._now()
            await emit("status", {"step": "gravando", "msg": "Preparando sessão reproduzida…"})
            saving = asyncio.create_task(run_in_threadpool(
                store.save_initial,
                prompt=cached.prompt,
                result=result,
                original_image=original,
                prepared_image=prepared,
                usage=usages if usages else None,
                stage_timing=timings,
                replay_source=clean_name,
            ))
            try:
                record = await asyncio.shield(saving)
            except asyncio.CancelledError:
                with suppress(SessionError):
                    await saving
                raise
            save_ms = (
                record.versions[0].stage_timing[-1].ms
                if record.versions[0].stage_timing and record.versions[0].stage_timing[-1].step == "gravando"
                else max(0, round((llm._now() - save_started) * 1000))
            )
            await emit("stage_timing", {"step": "gravando", "ms": save_ms})
            await emit(
                "done",
                GenerationResult(
                    sid=record.sid,
                    version=1,
                    url=f"/app/{record.sid}/1",
                    summary=result.summary,
                ).model_dump(),
            )
        except SessionNotFound as error:
            await emit("error", ErrorResponse(code="not_found", msg=str(error)).model_dump())
        except (imaging.ImageProcessingError, llm.LLMError) as error:
            await emit("error", ErrorResponse(code="invalid_cache", msg=str(error)).model_dump())
        except SessionError as error:
            await emit("error", ErrorResponse(code="storage_error", msg=str(error)).model_dump())
        except Exception:
            logging.getLogger(__name__).error("cache replay failed unexpectedly")
            await emit("error", ErrorResponse(code="internal_error", msg="Não foi possível reproduzir a gravação.").model_dump())

    return _run_generation_stream(produce)


_VENDOR_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bsrc=["\']/vendor/([A-Za-z0-9._-]+)["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)
_VENDOR_LINK_RE = re.compile(
    r'<link\b[^>]*\bhref=["\']/vendor/([A-Za-z0-9._-]+)["\'][^>]*>',
    re.IGNORECASE,
)


_VENDOR_FONT_URL_RE = re.compile(r'url\(["\']?/vendor/([A-Za-z0-9._-]+\.woff2)["\']?\)')


def _inline_vendor_assets(html: str) -> str:
    vendor_dir = (BASE_DIR / "web" / "vendor").resolve()

    def replace_font_url(match: re.Match[str]) -> str:
        font_name = match.group(1)
        font_path = (vendor_dir / font_name).resolve()
        if font_path.parent != vendor_dir or not font_path.is_file():
            return match.group(0)
        try:
            encoded = base64.b64encode(font_path.read_bytes()).decode("ascii")
        except OSError:
            return match.group(0)
        return f'url("data:font/woff2;base64,{encoded}")'

    def replace_script(match: re.Match[str]) -> str:
        name = match.group(1)
        path = (vendor_dir / name).resolve()
        if path.parent != vendor_dir or not path.is_file():
            return match.group(0)
        try:
            script_text = path.read_text(encoding="utf-8")
        except OSError:
            return match.group(0)
        safe_script = script_text.replace("</script", "<\\/script")
        return f"<script>\n{safe_script}\n</script>"

    def replace_link(match: re.Match[str]) -> str:
        name = match.group(1)
        if not name.lower().endswith(".css"):
            return match.group(0)
        path = (vendor_dir / name).resolve()
        if path.parent != vendor_dir or not path.is_file():
            return match.group(0)
        try:
            css_text = path.read_text(encoding="utf-8")
        except OSError:
            return match.group(0)
        css_text = _VENDOR_FONT_URL_RE.sub(replace_font_url, css_text)
        safe_css = css_text.replace("</style", "<\\/style")
        return f"<style>\n{safe_css}\n</style>"

    inlined = _VENDOR_SCRIPT_RE.sub(replace_script, html)
    return _VENDOR_LINK_RE.sub(replace_link, inlined)


class VendorStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response


@app.get("/api/prompts", response_model=PromptsResponse)
def get_prompts():
    return PromptsResponse(
        generate=prompts.SYSTEM_GENERATE,
        generate_text=prompts.SYSTEM_GENERATE_TEXT,
        chat=prompts.SYSTEM_CHAT,
        extras=prompts.SYSTEM_EXTRAS,
    )


@app.get("/api/sessions/{sid}/image")
def get_session_image(sid: str):
    try:
        image_bytes = store.read_prepared_image(sid)
    except SessionNotFound as error:
        return _error(404, "not_found", str(error))
    except SessionError as error:
        return _error(500, "storage_error", str(error))
    if image_bytes is None:
        return _error(404, "not_found", "Esta sessão não possui imagem de desenho.")
    return Response(
        content=image_bytes,
        media_type="image/jpeg",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/sessions/{sid}/download/{n}", response_class=HTMLResponse)
def download_version_html(sid: str, n: Annotated[int, PathParameter(ge=1)]):
    try:
        html = store.read_version(sid, n)
    except SessionNotFound as error:
        return _error(404, "not_found", str(error))
    except SessionError as error:
        return _error(500, "storage_error", str(error))
    standalone = _inline_vendor_assets(html)
    return HTMLResponse(
        standalone,
        headers={
            "Content-Disposition": f'attachment; filename="sketch2app-{sid}-v{n}.html"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/hub", response_model=HubListResponse)
def list_hub_projects():
    backend = storage_mod.get_storage(store)
    return HubListResponse(
        backend=backend.backend_name,
        items=backend.list_sessions(),
    )


@app.get("/hub", response_class=HTMLResponse)
def hub_page():
    hub_file = BASE_DIR / "web" / "hub.html"
    return HTMLResponse(
        hub_file.read_text(encoding="utf-8"),
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def _client_rate_key(request: Request) -> str:
    cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf_ip:
        return cf_ip
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",", 1)[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


@app.get("/enviar", response_class=HTMLResponse)
def audience_upload_page():
    enviar_file = BASE_DIR / "web" / "enviar.html"
    return HTMLResponse(
        enviar_file.read_text(encoding="utf-8"),
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.post("/upload", response_model=QueueItem, status_code=201)
async def upload_audience_drawing(request: Request, payload: UploadDrawingRequest):
    client_key = _client_rate_key(request)
    try:
        item = await run_in_threadpool(
            audience_queue.enqueue,
            image_b64=payload.image_b64,
            author=payload.author or "",
            note=payload.note or "",
            client_key=client_key,
        )
    except audience_mod.AudienceUploadError as error:
        return _error(error.status_code, error.code, error.msg)
    return item


@app.get("/api/queue", response_model=AudienceQueueResponse)
def list_audience_queue():
    public_url = audience_mod.get_public_upload_url()
    qr_svg = audience_mod.get_public_qr_svg(public_url)
    return AudienceQueueResponse(
        public_url=public_url,
        qr_svg=qr_svg,
        items=audience_queue.list_items(),
    )


@app.get("/api/queue/{qid}/image")
def get_audience_queue_image(qid: str):
    try:
        image_bytes = audience_queue.read_image(qid)
    except audience_mod.AudienceUploadError as error:
        return _error(error.status_code, error.code, error.msg)
    return Response(
        content=image_bytes,
        media_type="image/jpeg",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.delete("/api/queue/{qid}")
def delete_audience_queue_item(qid: str):
    try:
        audience_queue.remove_item(qid)
    except audience_mod.AudienceUploadError as error:
        return _error(error.status_code, error.code, error.msg)
    return {"removed": qid}



_HEAD_OPEN_RE = re.compile(r"(<head\b[^>]*>)", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"(<html\b[^>]*>)", re.IGNORECASE)

_DATA_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
}


def _inject_session_sid(html: str, sid: str) -> str:
    script = f'<script>window.SKETCH_SID="{sid}";</script>'
    if script in html:
        return html
    if _HEAD_OPEN_RE.search(html):
        return _HEAD_OPEN_RE.sub(rf"\1{script}", html, count=1)
    if _HTML_OPEN_RE.search(html):
        return _HTML_OPEN_RE.sub(rf"\1{script}", html, count=1)
    return html


def _data_json(status: int, content: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=content, headers=_DATA_CORS_HEADERS)


def _ensure_session_exists(sid: str) -> None:
    try:
        store.load(sid)
    except SessionNotFound as exc:
        raise session_data_mod.SessionDataError("not_found", str(exc), 404) from exc
    except SessionError as exc:
        raise session_data_mod.SessionDataError("storage_error", str(exc), 500) from exc


async def _parse_data_json_body(request: Request) -> dict:
    try:
        raw = await request.body()
        if len(raw) > session_data_mod.MAX_ITEM_BYTES:
            raise session_data_mod.SessionDataError(
                "payload_too_large", "O item excede o limite de 16 KiB.", 413
            )
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except session_data_mod.SessionDataError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise session_data_mod.SessionDataError(
            "invalid_payload", "Corpo JSON inválido.", 422
        ) from exc
    if not isinstance(parsed, dict):
        raise session_data_mod.SessionDataError(
            "invalid_payload", "O corpo deve ser um objeto JSON.", 422
        )
    return parsed


@app.options("/api/data/{sid}/{colecao}")
@app.options("/api/data/{sid}/{colecao}/{item_id}")
def options_session_data(sid: str, colecao: str, item_id: str | None = None):
    return Response(status_code=204, headers=_DATA_CORS_HEADERS)


@app.get("/api/data/{sid}/{colecao}")
def get_session_data(sid: str, colecao: str):
    try:
        _ensure_session_exists(sid)
        items = data_store.list_items(sid, colecao)
    except session_data_mod.SessionDataError as error:
        return _data_json(
            error.status_code,
            ErrorResponse(code=error.code, msg=error.msg).model_dump(),
        )
    return _data_json(200, {"sid": sid, "collection": colecao, "items": items})


@app.post("/api/data/{sid}/{colecao}")
async def post_session_data(sid: str, colecao: str, request: Request):
    try:
        _ensure_session_exists(sid)
        body = await _parse_data_json_body(request)
        created = await run_in_threadpool(data_store.create_item, sid, colecao, body)
    except session_data_mod.SessionDataError as error:
        return _data_json(
            error.status_code,
            ErrorResponse(code=error.code, msg=error.msg).model_dump(),
        )
    return _data_json(201, created)


@app.put("/api/data/{sid}/{colecao}")
@app.put("/api/data/{sid}/{colecao}/{item_id}")
async def put_session_data(
    sid: str, colecao: str, request: Request, item_id: str | None = None
):
    try:
        _ensure_session_exists(sid)
        body = await _parse_data_json_body(request)
        updated = await run_in_threadpool(
            data_store.upsert_item, sid, colecao, body, item_id=item_id
        )
    except session_data_mod.SessionDataError as error:
        return _data_json(
            error.status_code,
            ErrorResponse(code=error.code, msg=error.msg).model_dump(),
        )
    return _data_json(200, updated)


@app.delete("/api/data/{sid}/{colecao}")
@app.delete("/api/data/{sid}/{colecao}/{item_id}")
async def delete_session_data(
    sid: str, colecao: str, request: Request, item_id: str | None = None
):
    query_id = request.query_params.get("id")
    target_id = item_id if item_id is not None else query_id
    try:
        _ensure_session_exists(sid)
        deleted = await run_in_threadpool(
            data_store.delete_items, sid, colecao, item_id=target_id
        )
    except session_data_mod.SessionDataError as error:
        return _data_json(
            error.status_code,
            ErrorResponse(code=error.code, msg=error.msg).model_dump(),
        )
    return _data_json(200, {"deleted": deleted})


@app.get("/app/{sid}/{n}", response_class=HTMLResponse)
def version_html(sid: str, n: Annotated[int, PathParameter(ge=1)]):
    try:
        html = store.read_version(sid, n)
    except SessionNotFound as error:
        return _error(404, "not_found", str(error))
    except SessionError as error:
        return _error(500, "storage_error", str(error))
    served_html = _inject_session_sid(html, sid)
    return HTMLResponse(served_html, headers={
        "Content-Security-Policy": "sandbox allow-scripts",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })


app.mount("/vendor", VendorStaticFiles(directory=BASE_DIR / "web" / "vendor"), name="vendor")
app.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")
