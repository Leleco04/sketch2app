"""Persistência real em diretórios temporários, sem rede."""

import json
from pathlib import Path

import pytest

from app.llm import ParsedOutput
from app.sessions import SessionError, SessionNotFound, SessionStore

RESULT = ParsedOutput(summary="Criei o app.", html="<!DOCTYPE html><html><body>App</body></html>")


def save(store, **kwargs):
    return store.save_initial(prompt="paleta azul", result=RESULT, **kwargs)


def test_session_survives_new_store_instance(tmp_path):
    root = tmp_path / "generated"
    first = save(SessionStore(root))
    restarted = SessionStore(root)
    loaded = restarted.load(first.sid)
    assert loaded == first
    assert loaded.current == 1
    assert [version.n for version in loaded.versions] == [1]
    assert loaded.created_at.utcoffset().total_seconds() == 0
    assert restarted.read_version(first.sid, 1) == RESULT.html
    metadata = json.loads((root / first.sid / "session.json").read_text(encoding="utf-8"))
    assert [message["role"] for message in metadata["messages"]] == ["user", "assistant"]
    assert metadata["messages"][1]["text"] == RESULT.summary
    assert "<!DOCTYPE" not in json.dumps(metadata)
    assert metadata["original_image"] is None


def test_images_are_persisted_separately(tmp_path):
    store = SessionStore(tmp_path)
    record = save(store, original_image=b"original-png", prepared_image=b"prepared-jpeg")
    assert record.original_image == "original.bin"
    assert record.prepared_image == "image.jpg"
    assert (tmp_path / record.sid / "original.bin").read_bytes() == b"original-png"
    assert (tmp_path / record.sid / "image.jpg").read_bytes() == b"prepared-jpeg"


def test_multiple_sessions_are_independent(tmp_path):
    store = SessionStore(tmp_path)
    first, second = save(store), save(store)
    assert first.sid != second.sid
    assert store.load(first.sid).sid == first.sid
    assert store.read_version(second.sid, 1) == RESULT.html


def test_publication_occurs_only_after_all_files_are_written(tmp_path, monkeypatch):
    original_rename = Path.rename
    published = []

    def inspect_staging(source, target):
        assert source.resolve().parent == target.resolve().parent == tmp_path.resolve()
        assert (source / "session.json").is_file()
        assert (source / "v1.html").read_text(encoding="utf-8") == RESULT.html
        assert not target.exists()
        published.append(target)
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", inspect_staging)
    record = save(SessionStore(tmp_path))
    assert published == [tmp_path / record.sid]
    assert not list(tmp_path.glob(".pending-*"))


def test_failed_write_does_not_publish_or_leave_partial_session(tmp_path, monkeypatch):
    original_write = Path.write_text

    def fail_metadata(path, *args, **kwargs):
        if path.name == "session.json":
            raise OSError("private-disk-path")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_metadata)
    with pytest.raises(SessionError) as error:
        save(SessionStore(tmp_path), original_image=b"png", prepared_image=b"jpeg")
    assert "private-disk-path" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("images", [{"original_image": b"png"}, {"prepared_image": b"jpeg"}])
def test_incomplete_image_pair_is_rejected_before_writing(tmp_path, images):
    with pytest.raises(SessionError):
        save(SessionStore(tmp_path), **images)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("sid", ["..", "../outside", "a" * 31, "A" * 32, "a" * 32 + "/..", r"C:\private", None])
def test_invalid_session_id_never_reads_disk(tmp_path, monkeypatch, sid):
    def forbidden(*args, **kwargs):
        raise AssertionError("Identificador inválido não deve ler arquivos.")

    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(SessionNotFound):
        SessionStore(tmp_path).load(sid)


def test_missing_session_or_version_is_not_found(tmp_path):
    store = SessionStore(tmp_path)
    with pytest.raises(SessionNotFound):
        store.load("f" * 32)
    record = save(store)
    (tmp_path / record.sid / "v2.html").write_text("outside metadata", encoding="utf-8")
    with pytest.raises(SessionNotFound):
        store.read_version(record.sid, 2)
    (tmp_path / record.sid / "v1.html").unlink()
    with pytest.raises(SessionNotFound):
        store.read_version(record.sid, 1)


@pytest.mark.parametrize("version", [0, -1, True, 1.5, "1", "../session.json"])
def test_invalid_version_does_not_construct_a_file_path(tmp_path, version):
    with pytest.raises(SessionNotFound):
        SessionStore(tmp_path).read_version("a" * 32, version)


@pytest.mark.parametrize("change", [
    lambda data: data.update(current=2),
    lambda data: data["versions"].append(data["versions"][0]),
    lambda data: data.update(sid="f" * 32),
    lambda data: data.update(original_image="../private-file"),
    lambda data: data.update(created_at="2026-10-22T12:00:00"),
])
def test_inconsistent_metadata_is_rejected(tmp_path, change):
    store = SessionStore(tmp_path)
    record = save(store)
    path = tmp_path / record.sid / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionError):
        store.load(record.sid)


def test_corrupt_json_and_unreadable_html_have_controlled_errors(tmp_path):
    store = SessionStore(tmp_path)
    record = save(store)
    (tmp_path / record.sid / "v1.html").write_bytes(b"\xff")
    with pytest.raises(SessionError, match="HTML"):
        store.read_version(record.sid, 1)
    (tmp_path / record.sid / "session.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(SessionError, match="metadados"):
        store.load(record.sid)


def test_legacy_session_without_analysis_or_metrics_remains_readable(tmp_path):
    store = SessionStore(tmp_path)
    record = save(store)
    path = tmp_path / record.sid / "session.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["versions"] = [{"n": 1, "summary": "Versão antiga.", "ts": "2026-10-22T12:00:00Z"}]
    raw = json.dumps(data, indent=2)
    path.write_text(raw, encoding="utf-8")

    loaded = SessionStore(tmp_path).load(record.sid)
    assert loaded.versions[0].summary == "Versão antiga."
    assert loaded.versions[0].analysis is None
    assert loaded.versions[0].usage is None
    assert loaded.versions[0].stage_timing is None
    assert path.read_text(encoding="utf-8") == raw


def test_version_analysis_usage_and_stage_timings_survive_restart(tmp_path):
    store = SessionStore(tmp_path)
    parsed = ParsedOutput(
        summary="Criei o app.",
        html=RESULT.html,
        analysis=["Cabeçalho", "Lista de itens"],
    )
    saved = store.save_initial(
        prompt="paleta azul",
        result=parsed,
        usage=[{
            "attempt": 1,
            "model": "gemini-flash",
            "tokens_in": 210,
            "tokens_out": 540,
            "tokens_thinking": 32,
            "latency_ms": 1420,
            "wait_ms": 0,
            "calls_today": 1,
            "simulated": False,
        }],
        stage_timing=[
            {"step": "preparando", "ms": 12},
            {"step": "enviando", "ms": 340, "wait_ms": 0, "attempt": 1},
            {"step": "gerando", "ms": 1080, "attempt": 1},
            {"step": "validando", "ms": 3, "attempt": 1},
        ],
    )
    reloaded = SessionStore(tmp_path).load(saved.sid)
    assert reloaded == saved
    v1 = reloaded.versions[0]
    assert v1.analysis == ["Cabeçalho", "Lista de itens"]
    assert len(v1.usage) == 1
    assert v1.usage[0].model == "gemini-flash"
    assert v1.usage[0].tokens_in == 210
    assert [item.step for item in v1.stage_timing] == [
        "preparando", "enviando", "gerando", "validando", "gravando",
    ]


def test_save_version_appends_versions_messages_and_reads_prepared_image(tmp_path):
    store = SessionStore(tmp_path)
    initial = store.save_initial(
        prompt="inicial",
        result=RESULT,
        original_image=b"raw-bytes",
        prepared_image=b"prepared-jpeg",
    )
    assert store.read_prepared_image(initial.sid) == b"prepared-jpeg"

    v2_result = ParsedOutput("Tema escuro aplicado.", "<!DOCTYPE html><html><body>v2</body></html>")
    updated = store.save_version(
        initial.sid,
        prompt="modo escuro",
        result=v2_result,
        usage=[{
            "attempt": 1,
            "model": "mock",
            "tokens_in": 100,
            "tokens_out": 200,
            "tokens_thinking": None,
            "latency_ms": 200,
            "wait_ms": 0,
            "calls_today": 0,
            "simulated": True,
        }],
        stage_timing=[{"step": "preparando", "ms": 2}],
    )
    assert updated.current == 2
    assert [v.n for v in updated.versions] == [1, 2]
    assert [m.text for m in updated.messages] == [
        "inicial",
        RESULT.summary,
        "modo escuro",
        "Tema escuro aplicado.",
    ]
    assert store.read_version(initial.sid, 1) == RESULT.html
    assert store.read_version(initial.sid, 2) == v2_result.html
    assert [t.step for t in updated.versions[1].stage_timing] == ["preparando", "gravando"]

    text_only = store.save_initial(prompt="só texto", result=RESULT)
    assert store.read_prepared_image(text_only.sid) is None


def test_save_version_rolls_back_html_if_json_replace_fails(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    initial = store.save_initial(prompt="inicial", result=RESULT)
    original_replace = Path.replace

    def fail_on_json(self, target):
        if self.name == ".pending-session.json":
            raise OSError("disk full")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_on_json)
    v2_result = ParsedOutput("v2", "<!DOCTYPE html><html><body>v2</body></html>")
    with pytest.raises(SessionError):
        store.save_version(initial.sid, prompt="ajuste", result=v2_result)

    reloaded = store.load(initial.sid)
    assert reloaded.current == 1
    assert [v.n for v in reloaded.versions] == [1]
    assert not (tmp_path / initial.sid / "v2.html").exists()
    assert not (tmp_path / initial.sid / ".pending-v2.html").exists()
    assert not (tmp_path / initial.sid / ".pending-session.json").exists()


def test_restore_version_persists_current_and_rejects_invalid_or_missing_html(tmp_path):
    store = SessionStore(tmp_path)
    initial = store.save_initial(prompt="inicial", result=RESULT)
    v2_result = ParsedOutput("v2", "<!DOCTYPE html><html><body>v2</body></html>")
    store.save_version(initial.sid, prompt="ajuste", result=v2_result)

    restored = store.restore_version(initial.sid, 1)
    assert restored.current == 1
    assert SessionStore(tmp_path).load(initial.sid).current == 1

    # Idempotente quando já está na versão solicitada.
    assert store.restore_version(initial.sid, 1).current == 1

    for invalid in (0, -1, True, 99):
        with pytest.raises(SessionNotFound):
            store.restore_version(initial.sid, invalid)

    (tmp_path / initial.sid / "v2.html").unlink()
    with pytest.raises(SessionNotFound):
        store.restore_version(initial.sid, 2)
    assert store.load(initial.sid).current == 1


def test_export_load_and_list_cache_with_and_without_metrics(tmp_path):
    store = SessionStore(tmp_path / "generated", tmp_path / "cache")
    initial = store.save_initial(
        prompt="lista de tarefas",
        result=ParsedOutput("Criei lista.", RESULT.html, ["Título", "Lista"]),
        original_image=b"raw-img",
        prepared_image=b"prep-img",
        usage=[{
            "attempt": 1,
            "model": "gemini-flash",
            "tokens_in": 120,
            "tokens_out": 400,
            "tokens_thinking": 10,
            "latency_ms": 900,
            "wait_ms": 0,
            "calls_today": 1,
            "simulated": False,
        }],
        stage_timing=[{"step": "preparando", "ms": 5}],
    )
    v2 = ParsedOutput("Modo escuro.", "<!DOCTYPE html><html><body>v2</body></html>")
    store.save_version(initial.sid, prompt="modo escuro", result=v2)

    exported = store.export_to_cache(initial.sid, "demo-lista")
    assert exported.exists()

    clean_name, cached = store.load_cached("demo-lista.json")
    assert clean_name == "demo-lista"
    assert len(cached.versions) == 2
    assert cached.versions[0].analysis == ["Título", "Lista"]
    assert cached.versions[0].usage[0].tokens_in == 120

    # Gravação antiga sem métricas opcionais continua válida.
    (tmp_path / "cache" / "legado.json").write_text(
        json.dumps({
            "prompt": "antigo",
            "versions": [{"n": 1, "summary": "Versão antiga.", "html": RESULT.html}],
            "messages": [{"role": "user", "text": "antigo"}, {"role": "assistant", "text": "Versão antiga."}],
        }),
        encoding="utf-8",
    )
    items = {item.name: item for item in store.list_cache()}
    assert set(items) == {"demo-lista", "legado"}
    assert items["demo-lista"].versions == 2
    assert items["demo-lista"].has_image is True
    assert items["demo-lista"].has_metrics is True
    assert items["legado"].has_metrics is False

    for bad_name in ("", "../segredo", "a/b", ".oculto"):
        with pytest.raises(SessionNotFound):
            store.load_cached(bad_name)
