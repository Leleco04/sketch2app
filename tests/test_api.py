"""Fluxo HTTP/ASGI com mock de execução, sem dependências de teste adicionais."""

import asyncio
import base64
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock
from urllib.parse import unquote

import httpx
import pytest
from PIL import Image

from app import llm, main, storage as storage_mod
from app.imaging import MAX_IMAGE_B64_CHARS
from app.sessions import SessionError, SessionStore


async def request(method, path, payload=None, *, raw=None, on_send=None, disconnect=None):
    body = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
    path, _, query = path.partition("?")
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect is not None:
            await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if on_send is not None:
            await on_send(message)

    await main.app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3" if disconnect is not None else "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": unquote(path), "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
    }, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    events = []
    if any(key == b"content-type" and value.startswith(b"text/event-stream") for key, value in start["headers"]):
        for block in content.decode().split("\n\n"):
            if block.startswith("event: "):
                name, data = block.split("\n", 1)
                events.append((name[7:], json.loads(data[6:])))
    return SimpleNamespace(
        events=events,
        status=start["status"], body=content,
        headers={key.decode(): value.decode() for key, value in start["headers"]},
        json=lambda: events[-1][1] if events else json.loads(content),
    )


def call(method, path, payload=None, **kwargs):
    return asyncio.run(request(method, path, payload, **kwargs))


@pytest.fixture(autouse=True)
def mock_api(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage_mod, "load_dotenv", lambda *args, **kwargs: None)
    for name, value in {
        "MOCK_LLM": "true", "OFFLINE_MODE": "false", "RECORD": "false", "GEMINI_API_KEY": "",
        "GEMINI_MODEL": "", "GEMINI_MODEL_FALLBACK": "", "THINKING_LEVEL": "",
        "THINKING_BUDGET": "", "MAX_IMAGE_SIDE": "64", "SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": "",
    }.items():
        monkeypatch.setenv(name, value)
    factory = Mock(side_effect=AssertionError("A API real não pode ser chamada nos testes."))
    monkeypatch.setattr(llm.genai, "Client", factory)
    monkeypatch.setattr(main, "store", SessionStore(tmp_path / "generated"))
    monkeypatch.setattr(main, "_generation_lock", asyncio.Lock())
    monkeypatch.setattr(llm, "_call_lock", asyncio.Lock())
    monkeypatch.setattr(llm, "_last_call_finished", None)
    monkeypatch.setattr(llm, "_daily_calls", 0)

    async def delay(seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(llm, "_sleep", delay)
    yield SimpleNamespace(root=main.store.root, factory=factory)
    factory.assert_not_called()


@pytest.mark.parametrize("mock,offline,expected", [
    ("true", "false", "mock"), ("false", "false", "live"),
    ("true", "true", "offline"), ("false", "true", "offline"),
])
def test_frontend_config_exposes_only_mode_and_size(monkeypatch, mock, offline, expected):
    monkeypatch.setenv("MOCK_LLM", mock)
    monkeypatch.setenv("OFFLINE_MODE", offline)
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "private-model")
    response = call("GET", "/api/config")
    assert response.status == 200
    assert response.json() == {"mode": expected, "max_image_bytes": 10 * 1024 * 1024}
    assert b"private" not in response.body


def test_frontend_config_invalid_settings_are_safe(monkeypatch):
    monkeypatch.setenv("MIN_INTERVAL_S", "invalid")
    response = call("GET", "/api/config")
    assert response.status == 500
    assert response.json()["code"] == "configuration"


@pytest.mark.parametrize("path,content_type", [
    ("/", "text/html"), ("/app.js", "javascript"),
    ("/style.css", "text/css"), ("/favicon.svg", "image/svg+xml"),
])
def test_frontend_assets_are_served_locally(path, content_type):
    response = call("GET", path)
    assert response.status == 200
    assert content_type in response.headers["content-type"]


def test_prompt_to_persisted_html_survives_store_restart(mock_api, monkeypatch):
    response = call("POST", "/api/sessions", {"prompt": "  paleta azul  "})
    assert response.status == 200
    result = response.json()
    assert result["version"] == 1
    assert result["url"] == f'/app/{result["sid"]}/1'
    assert "simulada" in result["summary"]
    record = main.store.load(result["sid"])
    assert record.prompt == "paleta azul"
    monkeypatch.setattr(main, "store", SessionStore(mock_api.root))
    page = call("GET", result["url"])
    assert page.status == 200
    assert page.body.startswith(b"<!DOCTYPE html>")
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == "sandbox allow-scripts"
    assert "allow-same-origin" not in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert llm._daily_calls == 0


def test_image_is_decoded_prepared_and_preserved_before_generation(mock_api, monkeypatch):
    source = BytesIO()
    Image.new("RGB", (128, 64), "red").save(source, format="PNG")
    original = source.getvalue()
    generate = AsyncMock(wraps=llm.generate_app)
    monkeypatch.setattr(llm, "generate_app", generate)
    response = call("POST", "/api/sessions", {"image_b64": base64.b64encode(original).decode("ascii")})
    assert response.status == 200
    result = response.json()
    directory = mock_api.root / result["sid"]
    assert (directory / "original.bin").read_bytes() == original
    prepared = (directory / "image.jpg").read_bytes()
    with Image.open(BytesIO(prepared)) as image:
        assert image.mode == "L"
        assert image.format == "JPEG"
        assert image.size == (64, 32)
    generate.assert_awaited_once_with(image=prepared, prompt="", on_event=ANY)
    assert main.store.load(result["sid"]).messages[0].text == "Desenho enviado."


@pytest.mark.parametrize("payload", [
    {}, {"prompt": "  "}, {"image_b64": ""}, {"prompt": 123},
    {"prompt": {"private-key": "private-input"}}, {"image_b64": ["private-image"]},
    {"prompt": "Teste", "unexpected": "private-input"}, {"prompt": "\ud800"},
])
def test_invalid_request_does_not_echo_input_or_create_session(mock_api, payload, caplog):
    response = call("POST", "/api/sessions", payload)
    assert response.status == 422
    assert response.json()["code"] == "invalid_request"
    assert "private-input" not in response.body.decode() + caplog.text
    assert "private-image" not in response.body.decode() + caplog.text
    assert not mock_api.root.exists()


def test_invalid_json_has_safe_contract(mock_api):
    response = call("POST", "/api/sessions", raw=b'{"prompt":"private-input"')
    assert response.status == 422
    assert set(response.json()) == {"code", "msg", "hint"}
    assert b"private-input" not in response.body


@pytest.mark.parametrize("image", ["private-invalid-base64", "data:image/png;base64,YWJj", "YWJj"])
def test_invalid_image_does_not_generate_or_persist(mock_api, image, monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(llm, "generate_app", generate)
    response = call("POST", "/api/sessions", {"image_b64": image, "prompt": "Teste"})
    assert response.status == 200
    assert response.events[-1][0] == "error"
    assert response.json()["code"] == "invalid_image"
    assert not mock_api.root.exists()
    generate.assert_not_awaited()


def test_oversized_base64_is_rejected_by_schema(mock_api):
    response = call("POST", "/api/sessions", {"image_b64": "A" * (MAX_IMAGE_B64_CHARS + 1)})
    assert response.status == 413
    assert response.json()["code"] == "image_too_large"
    assert not mock_api.root.exists()


@pytest.mark.parametrize("code", [
    "busy", "rate_limited", "timeout", "offline", "service_unavailable",
    "invalid_output", "truncated_output", "authentication", "configuration", "blocked",
])
def test_llm_errors_have_safe_sse_and_no_session(mock_api, monkeypatch, code):
    monkeypatch.setattr(llm, "generate_app", AsyncMock(side_effect=llm.LLMError(code, "Falha controlada", "Usar modo offline.")))
    response = call("POST", "/api/sessions", {"prompt": "Teste"})
    assert response.status == 200
    assert response.events[-1][0] == "error"
    assert response.json() == {"code": code, "msg": "Falha controlada", "hint": "Usar modo offline."}
    assert not mock_api.root.exists()
    assert not main._generation_lock.locked()


def test_offline_mode_prevents_session_creation_even_with_mock(mock_api, monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "true")
    response = call("POST", "/api/sessions", {"prompt": "Teste"})
    assert response.status == 200
    assert response.json()["code"] == "offline"
    assert not mock_api.root.exists()


def test_storage_failure_does_not_return_a_success_url(mock_api, monkeypatch):
    monkeypatch.setattr(main.store, "save_initial", Mock(side_effect=SessionError("Não foi possível gravar a sessão no disco.")))
    response = call("POST", "/api/sessions", {"prompt": "Teste"})
    assert response.status == 200
    assert response.json()["code"] == "storage_error"
    assert "url" not in response.json()
    assert not main._generation_lock.locked()


def test_concurrent_generation_is_rejected_before_sending_second_request(mock_api, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delay(seconds):
            entered.set()
            await release.wait()

        monkeypatch.setattr(llm, "_sleep", delay)
        first = asyncio.create_task(request("POST", "/api/sessions", {"prompt": "Primeiro"}))
        await entered.wait()
        try:
            second = await request("POST", "/api/sessions", {"prompt": "Segundo"})
            assert second.status == 200
            assert second.json()["code"] == "busy"
        finally:
            release.set()
            completed = await first
        assert completed.status == 200
        assert len(list(mock_api.root.iterdir())) == 1

    asyncio.run(scenario())


def test_cancelled_generation_does_not_publish_session_and_releases_lock(mock_api, monkeypatch):
    async def scenario():
        entered = asyncio.Event()

        async def delay(seconds):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(llm, "_sleep", delay)
        first = asyncio.create_task(request("POST", "/api/sessions", {"prompt": "Teste"}))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not main._generation_lock.locked()
        assert not llm._call_lock.locked()
        assert not mock_api.root.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/app/invalid/1", "/app/" + "f" * 32 + "/1", "/app/..%5cprivate/1"])
def test_unknown_or_unsafe_session_is_not_found(mock_api, path):
    response = call("GET", path)
    assert response.status == 404
    assert response.json()["code"] == "not_found"


def test_missing_invalid_and_corrupt_version_have_controlled_errors(mock_api):
    result = call("POST", "/api/sessions", {"prompt": "Teste"}).json()
    assert call("GET", f'/app/{result["sid"]}/2').status == 404
    assert call("GET", f'/app/{result["sid"]}/0').status == 422
    assert call("GET", f'/app/{result["sid"]}/not-a-number').status == 422
    (mock_api.root / result["sid"] / "session.json").write_text("{broken", encoding="utf-8")
    assert call("GET", result["url"]).json()["code"] == "storage_error"


def test_panel_and_local_tailwind_remain_accessible_without_exposing_generated_files(mock_api):
    assert call("GET", "/").status == 200
    vendor = call("GET", "/vendor/tailwind.js")
    assert vendor.status == 200
    assert len(vendor.body) > 100000
    result = call("POST", "/api/sessions", {"prompt": "Teste"}).json()
    assert call("GET", f'/generated/{result["sid"]}/session.json').status == 404


def test_sse_order_progress_and_done_after_persistence(mock_api):
    response = call("POST", "/api/sessions", {"prompt": "Teste"})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert [data["step"] for name, data in response.events if name == "status"] == [
        "preparando", "enviando", "gerando", "validando", "gravando",
    ]
    counts = [data["chars"] for name, data in response.events if name == "progress"]
    assert len(counts) == 4
    assert counts == sorted(set(counts))
    assert response.events[-1][0] == "done"
    html_path = mock_api.root / response.json()["sid"] / "v1.html"
    assert html_path.exists()
    assert b"<<<HTML>>>" not in response.body
    assert b"<<<END>>>" not in response.body
    code_streamed = "".join(data["chunk"] for name, data in response.events if name == "code")
    assert code_streamed == html_path.read_text(encoding="utf-8")
    assert [data["step"] for name, data in response.events if name == "stage_timing"] == [
        "preparando", "enviando", "gerando", "validando", "gravando",
    ]
    usages = [data for name, data in response.events if name == "usage"]
    assert len(usages) == 1 and usages[0]["simulated"] is True
    record = main.store.load(response.json()["sid"])
    assert record.versions[0].analysis == []
    assert len(record.versions[0].usage) == 1
    assert [t.step for t in record.versions[0].stage_timing] == [
        "preparando", "enviando", "gerando", "validando", "gravando",
    ]


def test_sse_with_image_emits_and_persists_analysis_and_metrics(mock_api):
    source = BytesIO()
    Image.new("RGB", (64, 64), "white").save(source, format="PNG")
    b64 = base64.b64encode(source.getvalue()).decode("ascii")
    response = call("POST", "/api/sessions", {"image_b64": b64, "prompt": "Lista"})
    assert response.status == 200
    assert response.events[-1][0] == "done"
    analysis_events = [data["items"] for name, data in response.events if name == "analysis"]
    assert len(analysis_events) == 1 and len(analysis_events[0]) == 3
    record = main.store.load(response.json()["sid"])
    assert record.versions[0].analysis == analysis_events[0]
    assert record.versions[0].usage[0].model == "mock"
    assert record.versions[0].usage[0].simulated is True


def test_sse_is_delivered_before_generation_finishes(mock_api, monkeypatch):
    async def scenario():
        received = asyncio.Event()

        async def generate(*args, on_event, **kwargs):
            await on_event("progress", {"chars": 5})
            await asyncio.wait_for(received.wait(), 1)
            return llm.ParsedOutput("Teste", "<!DOCTYPE html><html></html>")

        async def send(message):
            if b"event: progress" in message.get("body", b""):
                assert not mock_api.root.exists()
                received.set()

        monkeypatch.setattr(llm, "generate_app", generate)
        response = await request("POST", "/api/sessions", {"prompt": "Teste"}, on_send=send)
        assert response.events[-1][0] == "done"

    asyncio.run(scenario())


def test_send_disconnect_cancels_worker_and_releases_both_locks(mock_api):
    from starlette.requests import ClientDisconnect

    async def scenario():
        async def send(message):
            if b"event: progress" in message.get("body", b""):
                raise OSError("disconnected")

        with pytest.raises(ClientDisconnect):
            await request("POST", "/api/sessions", {"prompt": "Teste"}, on_send=send)
        assert not main._generation_lock.locked()
        assert not llm._call_lock.locked()
        assert not mock_api.root.exists()
        assert (await request("POST", "/api/sessions", {"prompt": "Novo"})).events[-1][0] == "done"

    asyncio.run(scenario())


def test_asgi23_disconnect_releases_locks(mock_api, monkeypatch):
    async def scenario():
        disconnect = asyncio.Event()
        monkeypatch.setattr(llm, "_sleep", lambda seconds: asyncio.sleep(0.01))

        async def send(message):
            if b"event: progress" in message.get("body", b""):
                disconnect.set()

        await request("POST", "/api/sessions", {"prompt": "Teste"}, on_send=send, disconnect=disconnect)
        assert not main._generation_lock.locked()
        assert not llm._call_lock.locked()
        assert not mock_api.root.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("asgi23", [False, True])
def test_disconnect_waits_for_started_persistence_before_releasing_lock(mock_api, monkeypatch, asgi23):
    async def scenario():
        disconnect, saving, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = main.run_in_threadpool

        async def threadpool(func, *args, **kwargs):
            if func == main.store.save_initial:
                saving.set()
                await release.wait()
            return await original(func, *args, **kwargs)

        async def send(message):
            if b'"step": "gravando"' in message.get("body", b""):
                await saving.wait()
                if asgi23:
                    disconnect.set()
                else:
                    raise OSError("disconnected")

        monkeypatch.setattr(main, "run_in_threadpool", threadpool)
        pending = asyncio.create_task(request("POST", "/api/sessions", {"prompt": "Teste"}, on_send=send,
                                              disconnect=disconnect if asgi23 else None))
        await saving.wait()
        await asyncio.sleep(0.02)
        try:
            assert main._generation_lock.locked()
            assert not pending.done()
        finally:
            release.set()
            from starlette.requests import ClientDisconnect
            try:
                await pending
            except ClientDisconnect:
                assert not asgi23
        assert not main._generation_lock.locked()
        assert len(list(mock_api.root.iterdir())) == 1

    asyncio.run(scenario())


def test_get_session_and_chat_messages_create_new_versions_with_and_without_reattach(mock_api, monkeypatch):
    raw = BytesIO()
    Image.new("RGB", (48, 32), "white").save(raw, format="PNG")
    created = call("POST", "/api/sessions", {"image_b64": base64.b64encode(raw.getvalue()).decode(), "prompt": "paleta azul"}).json()
    sid = created["sid"]

    detail = call("GET", f"/api/sessions/{sid}")
    assert detail.status == 200
    detail_json = detail.json()
    assert detail_json["sid"] == sid
    assert detail_json["current"] == 1
    assert detail_json["has_image"] is True
    assert len(detail_json["versions"]) == 1
    assert len(detail_json["messages"]) == 2

    observed_calls = []
    original_generate = llm.generate_app

    async def spy_generate(*args, **kwargs):
        observed_calls.append(kwargs)
        return await original_generate(*args, **kwargs)

    monkeypatch.setattr(llm, "generate_app", spy_generate)

    chat_v2 = call("POST", f"/api/sessions/{sid}/messages", {"text": "modo escuro", "reattach_image": False})
    assert chat_v2.status == 200
    assert chat_v2.events[-1][0] == "done"
    v2_done = chat_v2.json()
    assert v2_done["version"] == 2
    assert v2_done["url"] == f"/app/{sid}/2"
    assert observed_calls[0]["image"] is None
    assert observed_calls[0]["current_html"].startswith("<!DOCTYPE html>")
    assert len(observed_calls[0]["history"]) == 2

    chat_v3 = call("POST", f"/api/sessions/{sid}/messages", {"text": "esse retângulo era uma tabela", "reattach_image": True})
    assert chat_v3.status == 200
    v3_done = chat_v3.json()
    assert v3_done["version"] == 3
    assert v3_done["url"] == f"/app/{sid}/3"
    assert observed_calls[1]["image"] is not None
    assert len(observed_calls[1]["history"]) == 4

    assert call("GET", f"/app/{sid}/2").status == 200
    assert call("GET", f"/app/{sid}/3").status == 200

    updated_detail = call("GET", f"/api/sessions/{sid}").json()
    assert updated_detail["current"] == 3
    assert [v["n"] for v in updated_detail["versions"]] == [1, 2, 3]
    assert len(updated_detail["messages"]) == 6
    assert all("<<<HTML>>>" not in m["text"] for m in updated_detail["messages"])


def test_chat_validation_not_found_and_storage_errors(mock_api):
    missing_sid = "a" * 32
    get_missing = call("GET", f"/api/sessions/{missing_sid}")
    assert get_missing.status == 404
    assert get_missing.json()["code"] == "not_found"

    post_missing = call("POST", f"/api/sessions/{missing_sid}/messages", {"text": "modo escuro"})
    assert post_missing.status == 200
    assert post_missing.events[-1] == ("error", {"code": "not_found", "msg": "Sessão não encontrada.", "hint": None})

    bad_payload = call("POST", f"/api/sessions/{missing_sid}/messages", {"text": "   ", "secret": "nao-ecoar"})
    assert bad_payload.status == 422
    assert bad_payload.events[-1][0] == "error"
    assert bad_payload.json()["code"] == "invalid_request"
    assert b"nao-ecoar" not in bad_payload.body


def test_restore_version_does_not_call_llm_and_next_chat_uses_restored_html(mock_api, monkeypatch, caplog):
    import logging
    created = call("POST", "/api/sessions", {"prompt": "inicial"}).json()
    sid = created["sid"]
    # Personaliza v1 e v2 no disco para verificar qual HTML é enviado após o restore.
    (mock_api.root / sid / "v1.html").write_text("<!DOCTYPE html><html><body>HTML-V1</body></html>", encoding="utf-8")
    call("POST", f"/api/sessions/{sid}/messages", {"text": "ajuste para v2"})
    (mock_api.root / sid / "v2.html").write_text("<!DOCTYPE html><html><body>HTML-V2</body></html>", encoding="utf-8")

    fail_llm = AsyncMock(side_effect=AssertionError("Restore não pode chamar a LLM"))
    monkeypatch.setattr(llm, "generate_app", fail_llm)

    with caplog.at_level(logging.INFO):
        restored = call("POST", f"/api/sessions/{sid}/restore", {"version": 1})
    assert restored.status == 200
    restored_json = restored.json()
    assert restored_json["sid"] == sid
    assert restored_json["version"] == 1
    assert restored_json["url"] == f"/app/{sid}/1"
    assert fail_llm.await_count == 0
    assert f"session restore sid={sid} version=1 api_calls=0" in caplog.text
    assert call("GET", f"/api/sessions/{sid}").json()["current"] == 1

    observed = []
    original_generate = llm.generate_app.__wrapped__ if hasattr(llm.generate_app, "__wrapped__") else None
    # Restaura uma implementação controlada para inspecionar current_html na criação da v3.
    async def capture_generate(*args, **kwargs):
        observed.append(kwargs["current_html"])
        return llm.ParsedOutput("Resumo v3.", "<!DOCTYPE html><html><body>HTML-V3</body></html>")

    monkeypatch.setattr(llm, "generate_app", capture_generate)
    chat_v3 = call("POST", f"/api/sessions/{sid}/messages", {"text": "novo ajuste a partir da v1"})
    assert chat_v3.json()["version"] == 3
    assert observed == ["<!DOCTYPE html><html><body>HTML-V1</body></html>"]
    assert [v["n"] for v in call("GET", f"/api/sessions/{sid}").json()["versions"]] == [1, 2, 3]


def test_restore_version_validation_not_found_and_busy_errors(mock_api):
    created = call("POST", "/api/sessions", {"prompt": "inicial"}).json()
    sid = created["sid"]

    assert call("POST", f"/api/sessions/{sid}/restore", {"version": 99}).status == 404
    assert call("POST", f"/api/sessions/{'b' * 32}/restore", {"version": 1}).status == 404

    for invalid in ({"version": 0}, {"version": True}, {"version": "1"}, {"version": 1, "extra": "x"}):
        resp = call("POST", f"/api/sessions/{sid}/restore", invalid)
        assert resp.status == 422
        assert resp.json()["code"] == "invalid_request"

    async def check_busy():
        async with main._generation_lock:
            busy_resp = await request("POST", f"/api/sessions/{sid}/restore", {"version": 1})
            assert busy_resp.status == 409
            assert busy_resp.json()["code"] == "busy"

    asyncio.run(check_busy())


def test_record_true_saves_cache_and_offline_replay_runs_complete_with_chat_and_restore(mock_api, monkeypatch):
    monkeypatch.setenv("RECORD", "true")
    raw = BytesIO()
    Image.new("RGB", (48, 32), "white").save(raw, format="PNG")
    created = call("POST", "/api/sessions", {"image_b64": base64.b64encode(raw.getvalue()).decode(), "prompt": "paleta azul"}).json()
    recorded_sid = created["sid"]
    call("POST", f"/api/sessions/{recorded_sid}/messages", {"text": "modo escuro"})

    cache_list = call("GET", "/api/cache")
    assert cache_list.status == 200
    items = cache_list.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == recorded_sid
    assert items[0]["versions"] == 2
    assert items[0]["has_image"] is True
    assert items[0]["has_metrics"] is True

    # Simula operação 100% offline: nenhuma chamada a generate_app pode ocorrer.
    monkeypatch.setenv("OFFLINE_MODE", "true")
    monkeypatch.setenv("MOCK_LLM", "false")
    fail_llm = AsyncMock(side_effect=AssertionError("Replay offline não pode chamar generate_app"))
    monkeypatch.setattr(llm, "generate_app", fail_llm)

    replayed = call("POST", f"/api/replay/{recorded_sid}")
    assert replayed.status == 200
    assert replayed.events[-1][0] == "done"
    replayed_done = replayed.json()
    replayed_sid = replayed_done["sid"]
    assert replayed_sid != recorded_sid
    assert replayed_done["version"] == 1
    assert any(name == "analysis" for name, _ in replayed.events)
    assert any(name == "code" for name, _ in replayed.events)
    assert any(name == "usage" for name, _ in replayed.events)
    assert call("GET", f"/app/{replayed_sid}/1").status == 200

    # O chat na sessão reproduzida avança para a v2 gravada mesmo com OFFLINE_MODE=true.
    replayed_chat = call("POST", f"/api/sessions/{replayed_sid}/messages", {"text": "modo escuro"})
    assert replayed_chat.status == 200
    assert replayed_chat.events[-1][0] == "done"
    assert replayed_chat.json()["version"] == 2
    assert "modo escuro" in replayed_chat.json()["summary"]
    assert call("GET", f"/app/{replayed_sid}/2").status == 200

    # Restore também funciona sem chamar a LLM.
    restored = call("POST", f"/api/sessions/{replayed_sid}/restore", {"version": 1})
    assert restored.status == 200
    assert restored.json()["version"] == 1
    assert fail_llm.await_count == 0


def test_replay_legacy_cache_without_metrics_does_not_emit_fake_usage(mock_api):
    main.store.cache_root.mkdir(parents=True, exist_ok=True)
    (main.store.cache_root / "antiga.json").write_text(
        json.dumps({
            "prompt": "calculadora antiga",
            "versions": [{
                "n": 1,
                "summary": "Calculadora antiga.",
                "html": "<!DOCTYPE html><html><body>Calculadora</body></html>",
            }],
        }),
        encoding="utf-8",
    )
    replayed = call("POST", "/api/replay/antiga")
    assert replayed.status == 200
    assert replayed.events[-1][0] == "done"
    assert any(name == "code" for name, _ in replayed.events)
    assert any(name == "progress" for name, _ in replayed.events)
    assert not any(name == "usage" for name, _ in replayed.events)
    assert not any(name == "analysis" for name, _ in replayed.events)

    missing = call("POST", "/api/replay/nao-existe")
    assert missing.status == 200
    assert missing.events[-1] == ("error", {"code": "not_found", "msg": "Gravação não encontrada no cache.", "hint": None})


def test_stage_extras_prompts_session_image_and_standalone_download(mock_api):
    prompts_resp = call("GET", "/api/prompts")
    assert prompts_resp.status == 200
    prompts_data = prompts_resp.json()
    assert "ANALYSIS:" in prompts_data["generate"]
    assert "SUMMARY:" in prompts_data["generate_text"]
    assert "SUMMARY:" in prompts_data["chat"]

    source = BytesIO()
    Image.new("RGB", (64, 64), "white").save(source, format="PNG")
    img_b64 = base64.b64encode(source.getvalue()).decode("ascii")
    with_img = call("POST", "/api/sessions", {"image_b64": img_b64, "prompt": "paleta azul"})
    sid_img = with_img.json()["sid"]
    img_resp = call("GET", f"/api/sessions/{sid_img}/image")
    assert img_resp.status == 200
    assert img_resp.headers["content-type"].startswith("image/jpeg")
    assert img_resp.body.startswith(b"\xff\xd8")

    dl_resp = call("GET", f"/api/sessions/{sid_img}/download/1")
    assert dl_resp.status == 200
    assert "attachment" in dl_resp.headers["content-disposition"]
    dl_text = dl_resp.body.decode("utf-8")
    assert '<script src="/vendor/tailwind.js"></script>' not in dl_text
    assert "<script>" in dl_text

    text_only = call("POST", "/api/sessions", {"prompt": "apenas texto"})
    sid_txt = text_only.json()["sid"]
    assert call("GET", f"/api/sessions/{sid_txt}/image").status == 404


def test_vendor_cors_and_extras_mode_catalog_generation(mock_api):
    for asset in (
        "/vendor/tailwind.js",
        "/vendor/gsap-3.12.5.min.js",
        "/vendor/chart-4.4.7.umd.min.js",
        "/vendor/lucide-0.469.0.min.js",
        "/vendor/alpine-3.14.8.min.js",
        "/vendor/animate-4.1.1.min.css",
        "/vendor/fonts.css",
        "/vendor/inter-400.woff2",
    ):
        resp = call("GET", asset)
        assert resp.status == 200
        assert resp.headers.get("access-control-allow-origin") == "*"

    created = call("POST", "/api/sessions", {"prompt": "painel com gráfico", "use_extras": True})
    assert created.status == 200
    sid = created.json()["sid"]
    html_resp = call("GET", f"/app/{sid}/1")
    assert html_resp.status == 200
    html_text = html_resp.body.decode("utf-8")
    assert '/vendor/gsap-3.12.5.min.js' in html_text
    assert '/vendor/chart-4.4.7.umd.min.js' in html_text

    dl_resp = call("GET", f"/api/sessions/{sid}/download/1")
    dl_text = dl_resp.body.decode("utf-8")
    assert '/vendor/gsap-3.12.5.min.js' not in dl_text
    assert 'data:font/woff2;base64,' in dl_text


def test_hub_disk_and_supabase_storage_sync_and_cloud_pull(mock_api, tmp_path):
    created = call("POST", "/api/sessions", {"prompt": "meu projeto do hub"})
    sid = created.json()["sid"]
    call("POST", f"/api/sessions/{sid}/messages", {"text": "modo escuro"})

    hub_resp = call("GET", "/api/hub")
    assert hub_resp.status == 200
    hub_data = hub_resp.json()
    assert hub_data["backend"] == "disk"
    assert len(hub_data["items"]) == 1
    assert hub_data["items"][0]["sid"] == sid
    assert hub_data["items"][0]["versions"] == [1, 2]

    page_resp = call("GET", "/hub")
    assert page_resp.status == 200
    assert "Hub de" in page_resp.body.decode("utf-8")

    # Simula sincronização e pull do Supabase sem tocar na rede.
    cloud_objects: dict[str, bytes] = {}
    table_rows: dict[str, list] = {"projects": [], "versions": [], "messages": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/storage/v1/bucket/sketches":
            return httpx.Response(200, json={"id": "sketches"})
        if path.startswith("/storage/v1/object/list/sketches"):
            names = sorted({k.split("/")[0] for k in cloud_objects})
            return httpx.Response(200, json=[{"name": n} for n in names])
        if path.startswith("/storage/v1/object/sketches/"):
            key = path[len("/storage/v1/object/sketches/"):]
            if req.method == "POST":
                cloud_objects[key] = req.content
                return httpx.Response(200, json={"Key": key})
            if req.method == "GET":
                if key not in cloud_objects:
                    return httpx.Response(404, json={"error": "not_found"})
                content = cloud_objects[key]
                ct = "application/json" if key.endswith(".json") else "image/jpeg"
                return httpx.Response(200, content=content, headers={"Content-Type": ct})
        if path.startswith("/rest/v1/"):
            tbl = path[len("/rest/v1/"):]
            payload = json.loads(req.content.decode("utf-8"))
            if isinstance(payload, list):
                table_rows[tbl].extend(payload)
            else:
                table_rows[tbl].append(payload)
            return httpx.Response(201, json=payload)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = storage_mod.SupabaseSettings(
        url="https://example.supabase.co",
        service_key=storage_mod.SecretStr("secret-service-key"),
    )
    supa = storage_mod.SupabaseStorage(main.store, settings, http_client=client)
    supa.save_session(sid)
    assert f"{sid}/session.json" in cloud_objects
    assert len(table_rows["projects"]) == 1
    assert len(table_rows["versions"]) == 2
    assert len(table_rows["messages"]) == 4

    # Em um segundo SessionStore vazio, list_sessions e load_session baixam da nuvem.
    empty_store = SessionStore(tmp_path / "other_node")
    supa_other = storage_mod.SupabaseStorage(empty_store, settings, http_client=client)
    listed = supa_other.list_sessions()
    assert len(listed) == 1
    assert listed[0].sid == sid
    assert listed[0].synced_cloud is True
    loaded = supa_other.load_session(sid)
    assert loaded.current == 2
    assert empty_store.read_version(sid, 2).startswith("<!DOCTYPE html>")


async def _request_with_headers(method, path, payload=None, *, headers=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    path, _, query = path.partition("?")
    messages = []
    received = False
    raw_headers = [(b"content-type", b"application/json")]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("latin1"), v.encode("latin1")))

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await main.app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": unquote(path), "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": raw_headers,
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
    }, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return SimpleNamespace(
        status=start["status"],
        body=content,
        headers={key.decode(): value.decode() for key, value in start["headers"]},
        json=lambda: json.loads(content),
    )


def png_b64(size=(32, 32)):
    buf = BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def call_with_headers(method, path, payload=None, *, headers=None):
    return asyncio.run(_request_with_headers(method, path, payload, headers=headers))


def test_audience_upload_queue_and_qr(monkeypatch, tmp_path):
    from app import audience as audience_mod

    monkeypatch.setattr(
        main,
        "audience_queue",
        audience_mod.AudienceQueue(tmp_path / "queue", min_interval_s=8.0, max_items=3),
    )
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://palestra-demo.trycloudflare.com")

    page = call("GET", "/enviar")
    assert page.status == 200
    assert "Enviar Desenho ao Palco" in page.body.decode("utf-8")

    # Upload válido (PNG convertido para JPEG pré-processado via Pillow, sem chamar LLM)
    upload_res = call_with_headers(
        "POST",
        "/upload",
        {"image_b64": png_b64((48, 32)), "author": "Maria", "note": "tema neon azul"},
        headers={"Cf-Connecting-Ip": "198.51.100.10"},
    )
    assert upload_res.status == 201
    item = upload_res.json()
    qid = item["qid"]
    assert item["author"] == "Maria"
    assert item["note"] == "tema neon azul"

    # Rate limit para o mesmo IP
    rate_limited = call_with_headers(
        "POST",
        "/upload",
        {"image_b64": png_b64((48, 32)), "author": "Maria 2"},
        headers={"Cf-Connecting-Ip": "198.51.100.10"},
    )
    assert rate_limited.status == 429
    assert rate_limited.json()["code"] == "rate_limit"

    # Formato proibido (GIF) rejeitado por Pillow
    buf = BytesIO()
    Image.new("RGB", (24, 24), "white").save(buf, format="GIF")
    gif_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    bad_fmt = call_with_headers(
        "POST",
        "/upload",
        {"image_b64": gif_b64, "author": "Outro"},
        headers={"Cf-Connecting-Ip": "198.51.100.22"},
    )
    assert bad_fmt.status == 422
    assert bad_fmt.json()["code"] == "invalid_image"

    # Operador consulta a fila e o QR code SVG gerado para PUBLIC_BASE_URL
    queue_res = call("GET", "/api/queue")
    assert queue_res.status == 200
    qdata = queue_res.json()
    assert qdata["public_url"] == "https://palestra-demo.trycloudflare.com/enviar"
    assert qdata["qr_svg"].startswith("<svg ")
    assert len(qdata["items"]) == 1
    assert qdata["items"][0]["qid"] == qid

    # Imagem na fila já é JPEG pré-processado
    img_res = call("GET", f"/api/queue/{qid}/image")
    assert img_res.status == 200
    assert img_res.headers["content-type"] == "image/jpeg"
    assert img_res.body.startswith(b"\xff\xd8")

    # Operador descarta item da fila sem chamar LLM
    del_res = call("DELETE", f"/api/queue/{qid}")
    assert del_res.status == 200
    assert call("GET", "/api/queue").json()["items"] == []


def test_tunnel_guard_exposes_only_audience_and_app_routes(monkeypatch, tmp_path):
    from app import audience as audience_mod

    monkeypatch.setattr(
        main,
        "audience_queue",
        audience_mod.AudienceQueue(tmp_path / "queue", min_interval_s=0.0),
    )
    created = call("POST", "/api/sessions", {"prompt": "app para ver no celular"}).json()
    sid = created["sid"]

    tunnel_headers = {"X-Forwarded-For": "203.0.113.55", "Host": "demo.trycloudflare.com"}

    # Rotas permitidas pelo túnel: /enviar, POST /upload, /app/{sid}/{n}, /vendor/*
    assert call_with_headers("GET", "/enviar", headers=tunnel_headers).status == 200
    assert call_with_headers(
        "POST",
        "/upload",
        {"image_b64": png_b64((32, 32)), "author": "Plateia"},
        headers=tunnel_headers,
    ).status == 201
    assert call_with_headers("GET", f"/app/{sid}/1", headers=tunnel_headers).status == 200
    assert call_with_headers("GET", "/vendor/tailwind.js", headers=tunnel_headers).status == 200

    # Rotas bloqueadas pelo túnel (nunca expor painel, geração, chat, fila ou hub)
    for method, blocked_path, payload in [
        ("GET", "/", None),
        ("GET", "/index.html", None),
        ("GET", "/hub", None),
        ("GET", "/api/config", None),
        ("GET", "/api/queue", None),
        ("POST", "/api/sessions", {"prompt": "tentativa externa"}),
        ("POST", f"/api/sessions/{sid}/messages", {"text": "hack", "reattach_image": False}),
    ]:
        res = call_with_headers(method, blocked_path, payload, headers=tunnel_headers)
        assert res.status == 403, f"Esperava 403 para {method} {blocked_path}, obteve {res.status}"
        assert res.json()["code"] == "forbidden"


def test_session_data_api_crud_cors_and_sid_injection(monkeypatch, tmp_path):
    from app import session_data as session_data_mod

    monkeypatch.setattr(
        main,
        "data_store",
        session_data_mod.SessionDataStore(tmp_path / "session_data.sqlite3"),
    )

    observed_kwargs = []
    original_generate = llm.generate_app

    async def spy_generate(*args, **kwargs):
        observed_kwargs.append(kwargs)
        return await original_generate(*args, **kwargs)

    monkeypatch.setattr(llm, "generate_app", spy_generate)

    created = call("POST", "/api/sessions", {"prompt": "lista persistente", "use_data_api": True}).json()
    sid = created["sid"]
    assert observed_kwargs[0].get("use_data_api") is True

    # Verifica injeção de window.SKETCH_SID no HTML servido em /app/{sid}/1
    served = call("GET", f"/app/{sid}/1")
    assert served.status == 200
    assert f'window.SKETCH_SID="{sid}"' in served.body.decode("utf-8")

    # Preflight CORS (iframe sandbox sem allow-same-origin tem origem opaca)
    opt = call("OPTIONS", f"/api/data/{sid}/tarefas")
    assert opt.status == 204
    assert opt.headers.get("access-control-allow-origin") == "*"

    # CRUD completo em /api/data/{sid}/tarefas
    empty = call("GET", f"/api/data/{sid}/tarefas")
    assert empty.status == 200
    assert empty.headers.get("access-control-allow-origin") == "*"
    assert empty.json() == {"sid": sid, "collection": "tarefas", "items": []}

    post1 = call("POST", f"/api/data/{sid}/tarefas", {"titulo": "Testar telão", "feito": False})
    assert post1.status == 201
    assert post1.headers.get("access-control-allow-origin") == "*"
    item1 = post1.json()
    item_id = item1["id"]
    assert item1["titulo"] == "Testar telão"

    put1 = call("PUT", f"/api/data/{sid}/tarefas/{item_id}", {"titulo": "Testar telão", "feito": True})
    assert put1.status == 200
    assert put1.json()["feito"] is True

    listed = call("GET", f"/api/data/{sid}/tarefas").json()
    assert len(listed["items"]) == 1
    assert listed["items"][0] == {"id": item_id, "titulo": "Testar telão", "feito": True}

    deleted = call("DELETE", f"/api/data/{sid}/tarefas/{item_id}")
    assert deleted.status == 200
    assert deleted.json()["deleted"] == 1
    assert call("GET", f"/api/data/{sid}/tarefas").json()["items"] == []

    # Sessão inexistente retorna 404 com CORS
    missing = call("GET", f"/api/data/{'c' * 32}/tarefas")
    assert missing.status == 404
    assert missing.headers.get("access-control-allow-origin") == "*"
