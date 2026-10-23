"""Cliente Gemini e contrato de saída, com chamadas estritamente sequenciais."""

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from google.genai.client import AsyncClient
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from app.prompts import (
    SYSTEM_CHAT,
    SYSTEM_DATA_API,
    SYSTEM_EXTRAS,
    SYSTEM_GENERATE,
    SYSTEM_GENERATE_TEXT,
)

MAX_HTML_BYTES = 300 * 1024
MAX_ANALYSIS_ITEMS = 12
MAX_ANALYSIS_ITEM_CHARS = 80
ALLOWED_VENDOR_FILES = frozenset({
    "tailwind.js",
    "gsap-3.12.5.min.js",
    "chart-4.4.7.umd.min.js",
    "lucide-0.469.0.min.js",
    "alpine-3.14.8.min.js",
    "animate-4.1.1.min.css",
    "fonts.css",
    "inter-400.woff2",
    "space-grotesk-700.woff2",
    "jetbrains-mono-400.woff2",
})
_SCRIPT_SRC_RE = re.compile(
    r'<script\b[^>]*?\bsrc\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>"\']+))',
    re.IGNORECASE,
)
_LINK_HREF_RE = re.compile(
    r'<link\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>"\']+))',
    re.IGNORECASE,
)
_CODE_CHUNK_CHARS = 96
_CODE_FLUSH_INTERVAL_S = 0.15
_STREAM_TAIL_RE = re.compile(r"\s*(?:<<<END>>>|<{1,3}(?:E(?:N(?:D(?:>{0,2})?)?)?)?)?\s*$")
EventCallback = Callable[[str, dict], Awaitable[None]]


async def _notify(callback: EventCallback | None, event: str, **data) -> None:
    if callback is not None:
        await callback(event, data)


@dataclass(frozen=True, slots=True)
class ParsedOutput:
    summary: str
    html: str
    analysis: list[str] = field(default_factory=list)


class OutputParseError(ValueError):
    """Saída inválida; permite a única tentativa de correção da etapa 3."""

    code = "invalid_output"
    retryable = True


class OutputTruncatedError(OutputParseError):
    """Limite de tokens atingido; não repetir com a mesma configuração."""

    code = "truncated_output"
    retryable = False


def _parse_analysis(lines: list[str]) -> list[str]:
    """Extrai até 12 itens curtos do bloco opcional ANALYSIS; falhas devolvem []."""
    if not lines or not re.fullmatch(r"ANALYSIS:[ \t]*", lines[0].strip()):
        return []
    items: list[str] = []
    for raw in lines[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        bullet = re.match(r"^[-*•][ \t]*(.*)$", stripped)
        if bullet is None:
            return []
        text = " ".join(bullet.group(1).split())
        if text and len(items) < MAX_ANALYSIS_ITEMS:
            items.append(text[:MAX_ANALYSIS_ITEM_CHARS])
    return items


def parse_output(output: str, finish_reason: str | None = None) -> ParsedOutput:
    """Extrai análise opcional, resumo e documento; não executa nem sanitiza o HTML."""
    reason = str(getattr(finish_reason, "value", finish_reason) or "")
    if reason.rsplit(".", 1)[-1].upper() in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS"}:
        raise OutputTruncatedError(
            "A resposta atingiu MAX_OUTPUT_TOKENS e pode estar truncada. "
            "Simplifique o pedido ou ajuste o limite antes de tentar novamente."
        )
    if not isinstance(output, str) or not output.strip():
        raise OutputParseError("A resposta está vazia ou não contém texto.")
    try:
        output.encode("utf-8")
    except UnicodeEncodeError:
        raise OutputParseError("O HTML contém texto inválido para UTF-8.") from None

    header, start, remainder = output.strip().partition("<<<HTML>>>")
    html, end, trailing = remainder.rpartition("<<<END>>>")
    if not start or not end:
        raise OutputParseError("Faltam os delimitadores <<<HTML>>> e/ou <<<END>>>.")
    lines = re.split(r"\r\n|\r|\n", header.strip())
    analysis_lines: list[str] = []
    if len(lines) == 1:
        summary_line = lines[0]
    elif re.match(r"^ANALYSIS(?::|[ \t]|$)", lines[0]) and not any(
        line.lstrip().startswith("SUMMARY:") for line in lines[1:-1]
    ):
        analysis_lines = lines[:-1]
        summary_line = lines[-1]
    else:
        raise OutputParseError("Informe SUMMARY: seguido de um resumo em uma linha.")
    match = re.fullmatch(r"SUMMARY:[ \t]*([^\r\n]+)", summary_line.strip())
    if match is None or not match.group(1).strip():
        raise OutputParseError("Informe SUMMARY: seguido de um resumo em uma linha.")
    if trailing.strip():
        raise OutputParseError("Há conteúdo fora do bloco após <<<END>>>.")

    html = html.strip()
    size = len(html.encode("utf-8"))
    if size >= MAX_HTML_BYTES:
        raise OutputParseError("O HTML deve ter menos de 300 KB em UTF-8.")
    if not re.match(r"(?:<!DOCTYPE\s+html(?:\s|>)|<html(?:\s|>))", html, re.IGNORECASE):
        raise OutputParseError("O HTML deve começar com <!DOCTYPE html> ou <html>.")
    if not html.lower().endswith("</html>"):
        raise OutputParseError("O documento HTML está incompleto: falta </html> no final.")
    for pattern in (_SCRIPT_SRC_RE, _LINK_HREF_RE):
        for ref_match in pattern.finditer(html):
            ref = (ref_match.group(1) or ref_match.group(2) or ref_match.group(3) or "").strip()
            if not ref.startswith("/vendor/"):
                raise OutputParseError(
                    "O HTML contém <script src> ou <link href> fora de /vendor/. Use só o catálogo local em /vendor/."
                )
            asset_name = ref[len("/vendor/"):]
            if asset_name not in ALLOWED_VENDOR_FILES:
                raise OutputParseError(
                    f"Biblioteca '/vendor/{asset_name}' fora do catálogo permitido. Use só o catálogo local em /vendor/."
                )
    return ParsedOutput(
        summary=match.group(1).strip(),
        html=html,
        analysis=_parse_analysis(analysis_lines),
    )


class LLMError(RuntimeError):
    """Erro seguro para ser convertido em evento SSE nas próximas etapas."""

    def __init__(self, code: str, msg: str, hint: str | None = None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.hint = hint


class LLMSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, allow_inf_nan=False)

    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    model: str = ""
    fallback: str = ""
    max_output_tokens: int = Field(default=16000, gt=0)
    timeout_s: float = Field(default=60, gt=0)
    min_interval_s: float = Field(default=7, ge=0)
    history_turns: int = Field(default=4, ge=0)
    thinking_budget: int | None = Field(default=None, ge=-1)
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    record: bool = False
    offline: bool = False
    mock: bool = False

    @model_validator(mode="after")
    def validate_thinking_mode(self) -> "LLMSettings":
        if self.thinking_budget is not None and self.thinking_level is not None:
            raise ValueError("Configure apenas THINKING_LEVEL ou THINKING_BUDGET.")
        return self

    @classmethod
    def from_env(cls) -> "LLMSettings":
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        try:
            return cls(
                api_key=os.getenv("GEMINI_API_KEY", "").strip(),
                model=os.getenv("GEMINI_MODEL", "").strip(),
                fallback=os.getenv("GEMINI_MODEL_FALLBACK", "").strip(),
                max_output_tokens=os.getenv("MAX_OUTPUT_TOKENS", "16000"),
                timeout_s=os.getenv("LLM_TIMEOUT_S", "60"),
                min_interval_s=os.getenv("MIN_INTERVAL_S", "7"),
                history_turns=os.getenv("HISTORY_TURNS", "4"),
                thinking_budget=os.getenv("THINKING_BUDGET", "").strip() or None,
                thinking_level=os.getenv("THINKING_LEVEL", "").strip().lower() or None,
                record=os.getenv("RECORD", "false"),
                offline=os.getenv("OFFLINE_MODE", "false"),
                mock=os.getenv("MOCK_LLM", "false"),
            )
        except ValidationError:
            raise LLMError(
                "configuration", "Configuração inválida no .env. Confira os tipos e limites das variáveis."
            ) from None


logger = logging.getLogger(__name__)
_call_lock = asyncio.Lock()
_last_call_finished: float | None = None
_counter_day: date | None = None
_daily_calls = 0
_now = time.monotonic
_today = date.today
_sleep = asyncio.sleep
_OFFLINE_HINT = "Usar modo offline."
_FALLBACK_CODES = {"rate_limited", "service_unavailable", "timeout"}
_MOCK_DELAY_S = 0.2
_MOCK_ANALYSIS = (
    "ANALYSIS:\n"
    "- Rascunho recebido em modo de simulação (sem leitura real da imagem)\n"
    "- Cabeçalho e controles interativos de demonstração\n"
    "- Painel com contador para validar o fluxo local\n"
)


class _StreamTracker:
    """Agrupa análise, progresso e HTML incremental de uma única tentativa."""

    def __init__(self, attempt: int):
        self.attempt = attempt
        self.chars = 0
        self._buffer = ""
        self._analysis_checked = False
        self._html_offset: int | None = None
        self._html_emitted = 0
        self._pending_code = ""
        self._last_code_at: float | None = None

    def _collect_html(self) -> None:
        if self._html_offset is None:
            idx = self._buffer.find("<<<HTML>>>")
            if idx == -1:
                return
            self._html_offset = idx + len("<<<HTML>>>")
        if self._html_emitted == 0 and not self._pending_code:
            while self._html_offset < len(self._buffer) and self._buffer[self._html_offset].isspace():
                self._html_offset += 1
        body = self._buffer[self._html_offset:]
        available = _STREAM_TAIL_RE.sub("", body)
        if len(available) > self._html_emitted:
            self._pending_code += available[self._html_emitted:]
            self._html_emitted = len(available)

    async def feed(self, text: str, on_event: EventCallback | None) -> None:
        self.chars += len(text)
        self._buffer += text
        if not self._analysis_checked:
            summary_match = re.search(r"(?:^|\r\n|\r|\n)[ \t]*SUMMARY:", self._buffer)
            if summary_match is not None or "<<<HTML>>>" in self._buffer:
                self._analysis_checked = True
                prefix = (
                    self._buffer[:summary_match.start()]
                    if summary_match is not None
                    else self._buffer.partition("<<<HTML>>>")[0]
                )
                items = _parse_analysis(re.split(r"\r\n|\r|\n", prefix.strip())) if prefix.strip() else []
                if items:
                    await _notify(on_event, "analysis", items=items, attempt=self.attempt)
        await _notify(
            on_event,
            "progress",
            chars=self.chars,
            est_tokens=self.chars // 4,
            attempt=self.attempt,
        )
        self._collect_html()
        now = _now()
        if len(self._pending_code) >= _CODE_CHUNK_CHARS or (
            self._pending_code
            and self._last_code_at is not None
            and (now - self._last_code_at) >= _CODE_FLUSH_INTERVAL_S
        ):
            await _notify(on_event, "code", chunk=self._pending_code, attempt=self.attempt)
            self._pending_code = ""
            self._last_code_at = now
        elif self._pending_code and self._last_code_at is None:
            self._last_code_at = now

    async def finish(self, on_event: EventCallback | None) -> None:
        if not self._analysis_checked:
            self._analysis_checked = True
            summary_match = re.search(r"(?:^|\r\n|\r|\n)[ \t]*SUMMARY:", self._buffer)
            prefix = (
                self._buffer[:summary_match.start()]
                if summary_match is not None
                else self._buffer.partition("<<<HTML>>>")[0]
            )
            items = _parse_analysis(re.split(r"\r\n|\r|\n", prefix.strip())) if prefix.strip() else []
            if items:
                await _notify(on_event, "analysis", items=items, attempt=self.attempt)
        self._collect_html()
        if self._pending_code:
            await _notify(on_event, "code", chunk=self._pending_code, attempt=self.attempt)
            self._pending_code = ""


def _mock_output(prompt: str) -> str:
    """Resposta fixa; a instrução só varia o resumo, nunca é inserida no HTML."""
    instruction = " ".join(prompt.split()).replace("<", "‹").replace(">", "›")
    summary = "Prévia simulada para desenvolvimento."
    if instruction:
        summary += " Instrução: " + instruction[:160]
    html = Path(__file__).with_name("mock.html").read_text(encoding="utf-8")
    return f"SUMMARY: {summary}\n<<<HTML>>>\n{html}\n<<<END>>>"


_MOCK_EXTRAS_HEAD = (
    '  <link rel="stylesheet" href="/vendor/fonts.css">\n'
    '  <link rel="stylesheet" href="/vendor/animate-4.1.1.min.css">\n'
    '  <script src="/vendor/gsap-3.12.5.min.js"></script>\n'
    '  <script src="/vendor/chart-4.4.7.umd.min.js"></script>\n'
    '  <script src="/vendor/lucide-0.469.0.min.js"></script>\n'
)


async def _generate_mock(
    prompt: str,
    on_event: EventCallback | None = None,
    *,
    has_image: bool = False,
    use_extras: bool = False,
) -> ParsedOutput:
    """Simula latência sob o lock, sem tocar nas métricas da API real."""
    started = _now()
    outcome = "cancelled"
    try:
        send_started = _now()
        await _notify(on_event, "status", step="enviando", msg="Preparando a simulação (sem API).", attempt=1)
        await _notify(on_event, "status", step="gerando", msg="Gerando prévia simulada…", attempt=1)
        output = _mock_output(prompt)
        if use_extras and isinstance(output, str) and '<script src="/vendor/tailwind.js"></script>' in output:
            output = output.replace(
                '  <script src="/vendor/tailwind.js"></script>\n',
                '  <script src="/vendor/tailwind.js"></script>\n' + _MOCK_EXTRAS_HEAD,
                1,
            ).replace(
                "  </main>\n",
                '    <canvas id="mock-chart" height="90" aria-label="Gráfico simulado"></canvas>\n  </main>\n',
                1,
            ).replace(
                "  </script>\n",
                "    if (window.gsap) window.gsap.from('main', { opacity: 0.85, y: 8, duration: 0.25 });\n"
                "    const chartEl = document.getElementById('mock-chart');\n"
                "    if (window.Chart && chartEl) {\n"
                "      new window.Chart(chartEl, { type: 'bar', data: { labels: ['v1', 'v2', 'v3'], datasets: [{ label: 'Interações', data: [4, 9, 14], backgroundColor: '#2563eb' }] }, options: { animation: false } });\n"
                "    }\n"
                "    if (window.lucide) window.lucide.createIcons();\n"
                "  </script>\n",
                1,
            )
        if has_image and isinstance(output, str) and output.startswith("SUMMARY:"):
            output = _MOCK_ANALYSIS + output
        if on_event is None:
            await _sleep(_MOCK_DELAY_S)
        else:
            send_ms = max(0, round((_now() - send_started) * 1000))
            await _notify(on_event, "stage_timing", step="enviando", ms=send_ms, wait_ms=0, attempt=1)
            gen_started = _now()
            tracker = _StreamTracker(attempt=1)
            # Quatro blocos explicitamente simulados, sob o mesmo lock.
            size = max(1, (len(output) + 3) // 4)
            for start_idx in range(0, len(output), size):
                await _sleep(_MOCK_DELAY_S / 4)
                await tracker.feed(output[start_idx:start_idx + size], on_event)
            await tracker.finish(on_event)
            gen_ms = max(0, round((_now() - gen_started) * 1000))
            await _notify(on_event, "stage_timing", step="gerando", ms=gen_ms, attempt=1)
        await _notify(on_event, "status", step="validando", msg="Validando o documento completo…", attempt=1)
        val_started = _now()
        try:
            result = parse_output(output, finish_reason="STOP")
        finally:
            val_ms = max(0, round((_now() - val_started) * 1000))
            await _notify(on_event, "stage_timing", step="validando", ms=val_ms, attempt=1)
        await _notify(
            on_event,
            "usage",
            attempt=1,
            model="mock",
            tokens_in=max(1, len(prompt) // 4) if prompt.strip() else 0,
            tokens_out=max(1, len(output) // 4),
            tokens_thinking=None,
            latency_ms=max(0, round((_now() - started) * 1000)),
            wait_ms=0,
            calls_today=_daily_calls,
            simulated=True,
        )
        outcome = "completed"
        return result
    except OutputParseError as error:
        outcome = error.code
        raise LLMError(error.code, str(error)) from None
    except OSError:
        outcome = "mock_error"
        raise LLMError("mock_error", "Não foi possível carregar a prévia simulada.") from None
    finally:
        logger.info(
            "llm mode=mock latency_s=%.3f api_calls=0 status=%s", _now() - started, outcome
        )


_REPLAY_DELAY_S = 0.08


async def stream_recorded_version(
    *,
    summary: str,
    html: str,
    analysis: list[str] | None = None,
    usage: Sequence[object] | None = None,
    stage_timing: Sequence[object] | None = None,
    is_initial: bool = True,
    on_event: EventCallback | None = None,
) -> ParsedOutput:
    """Reconstrói o streaming de uma versão gravada no cache, sem chamadas à API."""
    async with _call_lock:
        started = _now()
        outcome = "cancelled"
        try:
            clean_summary = " ".join(summary.split()) or "Versão gravada em cache."
            if is_initial and analysis:
                header = "ANALYSIS:\n" + "\n".join(f"- {item}" for item in analysis) + f"\nSUMMARY: {clean_summary}\n"
            else:
                header = f"SUMMARY: {clean_summary}\n"
            output = f"{header}<<<HTML>>>\n{html.strip()}\n<<<END>>>"

            recorded_steps = {}
            if stage_timing:
                for entry in stage_timing:
                    step_name = entry["step"] if isinstance(entry, dict) else getattr(entry, "step", None)
                    if step_name:
                        recorded_steps[step_name] = (
                            entry if isinstance(entry, dict) else entry.model_dump()
                        )

            send_started = _now()
            await _notify(
                on_event, "status", step="enviando",
                msg="Carregando gravação offline (sem API)…", attempt=1,
            )
            await _notify(
                on_event, "status", step="gerando",
                msg="Reproduzindo geração gravada…", attempt=1,
            )
            if on_event is None:
                await _sleep(_REPLAY_DELAY_S)
            else:
                env_step = recorded_steps.get("enviando")
                await _notify(
                    on_event,
                    "stage_timing",
                    step="enviando",
                    ms=env_step["ms"] if env_step else max(0, round((_now() - send_started) * 1000)),
                    wait_ms=env_step.get("wait_ms", 0) if env_step else 0,
                    attempt=1,
                )
                gen_started = _now()
                tracker = _StreamTracker(attempt=1)
                size = max(1, (len(output) + 3) // 4)
                for start_idx in range(0, len(output), size):
                    await _sleep(_REPLAY_DELAY_S / 4)
                    await tracker.feed(output[start_idx:start_idx + size], on_event)
                await tracker.finish(on_event)
                gen_step = recorded_steps.get("gerando")
                await _notify(
                    on_event,
                    "stage_timing",
                    step="gerando",
                    ms=gen_step["ms"] if gen_step else max(0, round((_now() - gen_started) * 1000)),
                    attempt=1,
                )
            await _notify(on_event, "status", step="validando", msg="Validando o documento gravado…", attempt=1)
            val_started = _now()
            try:
                result = parse_output(output, finish_reason="STOP")
            finally:
                val_step = recorded_steps.get("validando")
                await _notify(
                    on_event,
                    "stage_timing",
                    step="validando",
                    ms=val_step["ms"] if val_step else max(0, round((_now() - val_started) * 1000)),
                    attempt=1,
                )
            if usage:
                for item in usage:
                    payload = item if isinstance(item, dict) else item.model_dump()
                    await _notify(on_event, "usage", **payload)
            outcome = "completed"
            return result
        except OutputParseError as error:
            outcome = error.code
            raise LLMError(error.code, str(error)) from None
        finally:
            logger.info(
                "llm mode=replay latency_s=%.3f api_calls=0 status=%s", _now() - started, outcome
            )


def _api_error(error: errors.APIError) -> LLMError:
    if error.code == 429:
        return LLMError("rate_limited", "Limite de chamadas do Gemini atingido.", _OFFLINE_HINT)
    if error.code == 503:
        return LLMError("service_unavailable", "O Gemini está temporariamente indisponível.", _OFFLINE_HINT)
    if error.code in {408, 504}:
        return LLMError("timeout", "O Gemini excedeu o tempo limite.", _OFFLINE_HINT)
    if error.code in {401, 403}:
        return LLMError("authentication", "A chave não foi aceita ou não tem permissão. Confira o .env.")
    if error.code == 404:
        return LLMError("model_not_found", "Modelo indisponível. Confira GEMINI_MODEL no AI Studio.")
    return LLMError("api_error", "O Gemini recusou a chamada. Confira o modelo e a configuração no .env.")


async def _call_once(
    client: AsyncClient, settings: LLMSettings, model: str, contents: list[types.Content],
    on_event: EventCallback | None = None, *, attempt: int = 1, system_instruction: str = SYSTEM_GENERATE,
) -> types.GenerateContentResponse:
    """Uma tentativa; o chamador mantém o lock inclusive durante o intervalo."""
    global _last_call_finished, _counter_day, _daily_calls

    wait_ms = 0
    if _last_call_finished is not None:
        remaining = settings.min_interval_s - (_now() - _last_call_finished)
        if remaining > 0:
            wait_started = _now()
            await _notify(
                on_event, "status", step="enviando",
                msg="Aguardando o intervalo entre chamadas…", attempt=attempt,
            )
            await _sleep(remaining)
            wait_ms = max(0, round((_now() - wait_started) * 1000))

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=settings.max_output_tokens,
        candidate_count=1,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if settings.thinking_level is not None:
        config.thinking_config = types.ThinkingConfig(
            thinking_level=types.ThinkingLevel(settings.thinking_level.upper())
        )
    elif settings.thinking_budget is not None:
        config.thinking_config = types.ThinkingConfig(thinking_budget=settings.thinking_budget)

    day = _today()
    if _counter_day != day:
        _counter_day, _daily_calls = day, 0
    _daily_calls += 1
    started = _now()
    response = None
    outcome = "cancelled"
    error_to_raise: LLMError | None = None
    try:
        # Deadline total em asyncio; timeout HTTP fica desativado para não
        # depender das classes de exceção do transporte interno do SDK.
        async with asyncio.timeout(settings.timeout_s):
            await _notify(on_event, "status", step="enviando", msg="Enviando o pedido ao Gemini…", attempt=attempt)
            if on_event is None:
                response = await client.models.generate_content(
                    model=model, contents=contents, config=config
                )
            else:
                stream = await client.models.generate_content_stream(
                    model=model, contents=contents, config=config
                )
                chunks: list[str] = []
                tracker = _StreamTracker(attempt=attempt)
                first_text_at: float | None = None
                response = types.GenerateContentResponse()
                async with aclosing(stream):
                    await _notify(
                        on_event, "status", step="gerando",
                        msg="Recebendo o app do Gemini…", attempt=attempt,
                    )
                    async for chunk in stream:
                        if chunk.usage_metadata is not None:
                            response.usage_metadata = chunk.usage_metadata
                        if chunk.prompt_feedback is not None:
                            response.prompt_feedback = chunk.prompt_feedback
                        if chunk.candidates:
                            candidate = chunk.candidates[0]
                            if not response.candidates:
                                response.candidates = [types.Candidate()]
                            if candidate.finish_reason is not None:
                                response.candidates[0].finish_reason = candidate.finish_reason
                        text = _response_text(chunk)
                        if text:
                            if first_text_at is None:
                                first_text_at = _now()
                                send_ms = max(0, round((first_text_at - started) * 1000))
                                await _notify(
                                    on_event, "stage_timing",
                                    step="enviando", ms=send_ms, wait_ms=wait_ms, attempt=attempt,
                                )
                            chunks.append(text)
                            await tracker.feed(text, on_event)
                    await tracker.finish(on_event)
                stream_end = _now()
                if first_text_at is None:
                    send_ms = max(0, round((stream_end - started) * 1000))
                    await _notify(
                        on_event, "stage_timing",
                        step="enviando", ms=send_ms, wait_ms=wait_ms, attempt=attempt,
                    )
                    gen_ms = 0
                else:
                    gen_ms = max(0, round((stream_end - first_text_at) * 1000))
                await _notify(on_event, "stage_timing", step="gerando", ms=gen_ms, attempt=attempt)
                if response.candidates:
                    response.candidates[0].content = types.Content(
                        role="model", parts=[types.Part.from_text(text="".join(chunks))]
                    )
        outcome = "received"
    except TimeoutError:
        outcome = "timeout"
        error_to_raise = LLMError("timeout", "O Gemini excedeu o tempo limite.", _OFFLINE_HINT)
    except errors.APIError as error:
        error_to_raise = _api_error(error)
        outcome = error_to_raise.code
    except Exception:
        outcome = "network_error"
        error_to_raise = LLMError(
            "network_error", "Não foi possível concluir a comunicação com o Gemini.", _OFFLINE_HINT
        )
    finally:
        _last_call_finished = _now()
        call_latency_s = _last_call_finished - started
        usage = getattr(response, "usage_metadata", None)
        logger.info(
            "gemini model=%s tokens_in=%s tokens_out=%s tokens_thinking=%s latency_s=%.3f calls_today=%d status=%s",
            model,
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
            getattr(usage, "thoughts_token_count", None),
            call_latency_s,
            _daily_calls,
            outcome,
        )

    await _notify(
        on_event,
        "usage",
        attempt=attempt,
        model=model,
        tokens_in=getattr(usage, "prompt_token_count", None),
        tokens_out=getattr(usage, "candidates_token_count", None),
        tokens_thinking=getattr(usage, "thoughts_token_count", None),
        latency_ms=max(0, round(call_latency_s * 1000)),
        wait_ms=wait_ms,
        calls_today=_daily_calls,
        simulated=False,
    )
    if error_to_raise is not None:
        raise error_to_raise
    return response


def _response_text(response: types.GenerateContentResponse) -> str:
    candidates = response.candidates or []
    if not candidates or not candidates[0].content:
        return ""
    return "".join(
        part.text for part in candidates[0].content.parts or []
        if part.text and not part.thought
    )


def _parse_response(response: types.GenerateContentResponse) -> ParsedOutput:
    feedback = response.prompt_feedback
    if feedback and feedback.block_reason and str(feedback.block_reason.value) != "BLOCK_REASON_UNSPECIFIED":
        raise LLMError("blocked", "O Gemini bloqueou a geração. Tente outro desenho ou pedido.")
    candidates = response.candidates or []
    reason = candidates[0].finish_reason if candidates else None
    if reason and reason.value not in {"STOP", "MAX_TOKENS", "FINISH_REASON_UNSPECIFIED"}:
        raise LLMError("blocked", "O Gemini interrompeu a geração sem um documento completo.")
    return parse_output(_response_text(response), finish_reason=reason)


async def _request_with_fallback(
    client: AsyncClient, settings: LLMSettings, contents: list[types.Content], allow_retries: bool,
    on_event: EventCallback | None = None, *, attempt_start: int = 1,
    system_instruction: str = SYSTEM_GENERATE,
) -> tuple[types.GenerateContentResponse, bool, int]:
    try:
        return (
            await _call_once(
                client, settings, settings.model, contents, on_event,
                attempt=attempt_start, system_instruction=system_instruction,
            ),
            False,
            attempt_start,
        )
    except LLMError as error:
        if (
            not allow_retries or error.code not in _FALLBACK_CODES
            or not settings.fallback or settings.fallback == settings.model
        ):
            raise
    # Uma única troca. Uma falha do fallback encerra a operação.
    fallback_attempt = attempt_start + 1
    await _notify(
        on_event, "status", step="enviando",
        msg="Tentando o modelo alternativo uma vez…", attempt=fallback_attempt,
    )
    return (
        await _call_once(
            client, settings, settings.fallback, contents, on_event,
            attempt=fallback_attempt, system_instruction=system_instruction,
        ),
        True,
        fallback_attempt,
    )


async def _generate(
    client: AsyncClient, settings: LLMSettings, contents: list[types.Content], allow_retries: bool,
    on_event: EventCallback | None = None, *, system_instruction: str = SYSTEM_GENERATE,
) -> ParsedOutput:
    response, used_fallback, attempt = await _request_with_fallback(
        client, settings, contents, allow_retries, on_event,
        attempt_start=1, system_instruction=system_instruction,
    )
    await _notify(on_event, "status", step="validando", msg="Validando o documento completo…", attempt=attempt)
    val_started = _now()
    try:
        return _parse_response(response)
    except OutputParseError as error:
        if not error.retryable or not allow_retries or used_fallback:
            raise LLMError(error.code, str(error)) from None
        correction = (
            "Corrija a resposta anterior: " + str(error)
            + " Retorne SUMMARY: e o HTML completo entre <<<HTML>>> e <<<END>>>, sem texto externo."
        )
    finally:
        val_ms = max(0, round((_now() - val_started) * 1000))
        await _notify(on_event, "stage_timing", step="validando", ms=val_ms, attempt=attempt)
    repaired_contents = [
        *contents,
        types.Content(role="model", parts=[types.Part.from_text(text=_response_text(response))]),
        types.Content(role="user", parts=[types.Part.from_text(text=correction)]),
    ]
    next_attempt = attempt + 1
    await _notify(on_event, "status", step="enviando", msg="Corrigindo o formato uma vez…", attempt=next_attempt)
    repaired, _, final_attempt = await _request_with_fallback(
        client, settings, repaired_contents, allow_retries, on_event,
        attempt_start=next_attempt, system_instruction=system_instruction,
    )
    await _notify(
        on_event, "status", step="validando", msg="Validando o documento corrigido…", attempt=final_attempt
    )
    val_started = _now()
    try:
        return _parse_response(repaired)
    except OutputParseError as error:
        raise LLMError(error.code, str(error)) from None
    finally:
        val_ms = max(0, round((_now() - val_started) * 1000))
        await _notify(on_event, "stage_timing", step="validando", ms=val_ms, attempt=final_attempt)


async def generate_app(
    image: bytes | None = None, prompt: str = "", *,
    current_html: str | None = None,
    history: Sequence[object] | None = None,
    use_extras: bool = False,
    use_data_api: bool = False,
    allow_retries: bool = True,
    on_event: EventCallback | None = None,
) -> ParsedOutput:
    """Gera v1 ou ajusta versão existente via chat; não inicia o cliente no import.

    allow_retries=False limita o smoke test a exatamente uma tentativa de API.
    MOCK_LLM retorna a prévia simulada sem credenciais ou consumo de cota.
    OFFLINE_MODE tem prioridade e impede geração tanto real quanto simulada.
    """
    if _call_lock.locked():
        raise LLMError("busy", "Já existe uma geração em andamento. Aguarde sua conclusão.")
    settings = LLMSettings.from_env()
    if settings.offline:
        raise LLMError("offline", "O modo offline está ativo; nenhuma chamada foi enviada.", _OFFLINE_HINT)
    if not settings.mock and (not settings.api_key.get_secret_value() or not settings.model):
        raise LLMError("configuration", "Preencha GEMINI_API_KEY e GEMINI_MODEL no .env antes de gerar.")
    if image is not None and (not isinstance(image, bytes) or not image):
        raise LLMError("invalid_input", "O desenho deve conter bytes de um JPEG pré-processado.")

    is_chat = current_html is not None
    if is_chat:
        if not isinstance(current_html, str) or not current_html.strip():
            raise LLMError("invalid_input", "O HTML atual da versão é obrigatório para ajuste no chat.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMError("invalid_input", "Informe uma mensagem para ajustar o app.")
        contents: list[types.Content] = []
        if history and settings.history_turns > 0:
            for item in list(history)[-(settings.history_turns * 2):]:
                role = item["role"] if isinstance(item, dict) else getattr(item, "role", None)
                text = item["text"] if isinstance(item, dict) else getattr(item, "text", None)
                if role in {"user", "assistant"} and isinstance(text, str) and text.strip():
                    sdk_role = "user" if role == "user" else "model"
                    contents.append(
                        types.Content(role=sdk_role, parts=[types.Part.from_text(text=text.strip())])
                    )
        parts = []
        if image is not None:
            parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text=f"HTML ATUAL:\n{current_html}"))
        parts.append(types.Part.from_text(text=prompt.strip()))
        contents.append(types.Content(role="user", parts=parts))
        system_instruction = SYSTEM_CHAT
    else:
        if not isinstance(prompt, str) or (image is None and not prompt.strip()):
            raise LLMError("invalid_input", "Informe um desenho ou um pedido em texto.")
        parts = []
        if image is not None:
            parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
        if prompt.strip():
            parts.append(types.Part.from_text(text=prompt.strip()))
        contents = [types.Content(role="user", parts=parts)]
        system_instruction = SYSTEM_GENERATE if image is not None else SYSTEM_GENERATE_TEXT
    if use_extras:
        system_instruction = f"{system_instruction}\n\n{SYSTEM_EXTRAS}"
    if use_data_api:
        system_instruction = f"{system_instruction}\n\n{SYSTEM_DATA_API}"

    async with _call_lock:
        if settings.mock:
            return await _generate_mock(
                prompt, on_event, has_image=(image is not None and not is_chat), use_extras=use_extras
            )
        # Sem retries HTTP ocultos nem chamadas automáticas de ferramentas.
        try:
            with genai.Client(
                api_key=settings.api_key.get_secret_value(), vertexai=False, enterprise=False,
                http_options=types.HttpOptions(timeout=None, retry_options=types.HttpRetryOptions(attempts=1)),
            ) as sdk:
                async with sdk.aio as client:
                    return await _generate(
                        client, settings, contents, allow_retries, on_event,
                        system_instruction=system_instruction,
                    )
        except LLMError:
            raise
        except Exception:
            raise LLMError(
                "client_error", "Não foi possível preparar ou encerrar o cliente Gemini. Confira o .env."
            ) from None
