"""Validação do contrato por delimitadores, sem cliente Gemini ou rede."""

from enum import Enum

import pytest

from app.llm import (
    MAX_HTML_BYTES,
    OutputParseError,
    OutputTruncatedError,
    ParsedOutput,
    parse_output,
)

HTML = '<!DOCTYPE html>\n<html lang="pt-BR"><body>Olá!</body></html>'


def response(html: str = HTML, summary: str = "Criei uma lista de tarefas.") -> str:
    return f"SUMMARY: {summary}\n<<<HTML>>>\n{html}\n<<<END>>>"


@pytest.mark.parametrize("html", [HTML, "<html><body>App</body></html>", "<HTML></HTML>"])
def test_valid_document(html):
    result = parse_output(response(html), finish_reason="STOP")
    assert result.summary == "Criei uma lista de tarefas."
    assert result.html == html


def test_surrounding_whitespace_and_windows_newlines():
    result = parse_output(" \r\n" + response().replace("\n", "\r\n") + "\r\n ")
    assert result.summary == "Criei uma lista de tarefas."
    assert result.html == HTML.replace("\n", "\r\n")


@pytest.mark.parametrize("output", ["", "  ", None, 123])
def test_missing_text(output):
    with pytest.raises(OutputParseError, match="vazia"):
        parse_output(output)


@pytest.mark.parametrize(
    "output",
    [
        HTML,
        response().replace("<<<HTML>>>", ""),
        response().replace("<<<END>>>", ""),
        f"SUMMARY: App\n<<<END>>>\n{HTML}\n<<<HTML>>>",
    ],
)
def test_missing_or_reversed_delimiters(output):
    with pytest.raises(OutputParseError, match="delimitadores"):
        parse_output(output)


@pytest.mark.parametrize(
    "output",
    [
        response().replace("SUMMARY:", "RESUMO:"),
        response(summary=""),
        response(summary=" \t"),
        response(summary="Linha 1\nLinha 2"),
    ],
)
def test_invalid_summary(output):
    with pytest.raises(OutputParseError, match="SUMMARY"):
        parse_output(output)


@pytest.mark.parametrize(
    "prefix,suffix",
    [("```html\n", "\n```"), ("Explicação\n", ""), ("", "\nTexto extra")],
)
def test_content_outside_contract(prefix, suffix):
    with pytest.raises(OutputParseError):
        parse_output(prefix + response() + suffix)


@pytest.mark.parametrize(
    "html",
    [
        "", "<body>App</body></html>", "<htmlish></html>",
        "<!DOCTYPE svg></html>", "<html><body>App", HTML + "extra",
    ],
)
def test_incomplete_or_invalid_html(html):
    with pytest.raises(OutputParseError):
        parse_output(response(html))


@pytest.mark.parametrize("reason", ["MAX_TOKENS", "MAX_OUTPUT_TOKENS", "FinishReason.MAX_TOKENS"])
def test_truncation_never_allows_retry_even_with_complete_html(reason):
    with pytest.raises(OutputTruncatedError, match="MAX_OUTPUT_TOKENS") as error:
        parse_output(response(), finish_reason=reason)
    assert error.value.retryable is False
    assert error.value.code == "truncated_output"


def test_finish_reason_enum_and_truncation_before_parse():
    class FinishReason(str, Enum):
        MAX_TOKENS = "MAX_TOKENS"

    with pytest.raises(OutputTruncatedError):
        parse_output("", finish_reason=FinishReason.MAX_TOKENS)


def test_parse_error_can_be_used_for_single_correction_attempt():
    with pytest.raises(OutputParseError) as error:
        parse_output("invalid")
    assert error.value.retryable is True
    assert error.value.code == "invalid_output"


@pytest.mark.parametrize("delta,valid", [(-1, True), (0, False), (1, False)])
def test_size_limit_is_exclusive(delta, valid):
    wrapper = "<html></html>"
    html = "<html>" + "x" * (MAX_HTML_BYTES - len(wrapper) + delta) + "</html>"
    if valid:
        assert parse_output(response(html)).html == html
    else:
        with pytest.raises(OutputParseError, match="300 KB"):
            parse_output(response(html))


def test_size_counts_utf8_bytes_not_characters():
    html = "<html>" + "á" * (MAX_HTML_BYTES // 2) + "</html>"
    assert len(html) < MAX_HTML_BYTES
    with pytest.raises(OutputParseError, match="300 KB"):
        parse_output(response(html))


def test_invalid_unicode_has_controlled_error():
    with pytest.raises(OutputParseError, match="UTF-8"):
        parse_output(response("<html>\ud800</html>"))


def test_html_is_preserved_including_scripts_and_delimiter_literals():
    html = '<html><body><script>const end = "<<<END>>>"; const start = "<<<HTML>>>";</script></body></html>'
    result = parse_output(response(html))
    assert result.html == html
    assert result.analysis == []


def test_parsed_output_default_analysis_preserves_existing_constructor():
    parsed = ParsedOutput("Resumo", HTML)
    assert parsed.summary == "Resumo"
    assert parsed.html == HTML
    assert parsed.analysis == []


def test_valid_analysis_extracts_items_and_normalizes_whitespace():
    raw = (
        "ANALYSIS:\n"
        "-   Cabeçalho com título   e filtro \n"
        "* Botão adicionar tarefa (canto superior)\n"
        "• Lista com 3 itens de exemplo\n\n"
        + response()
    )
    result = parse_output(raw)
    assert result.analysis == [
        "Cabeçalho com título e filtro",
        "Botão adicionar tarefa (canto superior)",
        "Lista com 3 itens de exemplo",
    ]
    assert result.summary == "Criei uma lista de tarefas."
    assert result.html == HTML


def test_analysis_limits_to_12_items_and_80_chars_per_item():
    long_item = "Elemento " + ("x" * 100)
    bullets = "\n".join([f"- {long_item}"] + [f"- Item {i}" for i in range(2, 16)])
    result = parse_output(f"ANALYSIS:\n{bullets}\n{response()}")
    assert len(result.analysis) == 12
    assert len(result.analysis[0]) == 80
    assert result.analysis[0] == long_item[:80]
    assert result.analysis[-1] == "Item 12"


@pytest.mark.parametrize(
    "analysis_block",
    [
        "ANALYSIS:\n",
        "ANALYSIS:\n\n",
        "ANALYSIS:\n-\n-   \n",
        "ANALYSIS: item na mesma linha\n",
        "ANALYSIS sem dois pontos\n- Item 1\n",
        "ANALYSIS:\nTexto solto sem marcador\n",
        "ANALYSIS:\n- Item válido\nLinha quebrada sem marcador\n",
    ],
)
def test_malformed_analysis_returns_empty_list_without_error(analysis_block):
    result = parse_output(analysis_block + response())
    assert result.analysis == []
    assert result.summary == "Criei uma lista de tarefas."
    assert result.html == HTML


@pytest.mark.parametrize(
    "invalid_output",
    [
        "ANALYSIS:\n- Item 1\nSUMMARY: Linha 1\nLinha 2\n<<<HTML>>>\n" + HTML + "\n<<<END>>>",
        "ANALYSIS:\n- Item 1\n<<<HTML>>>\n" + HTML + "\n<<<END>>>",
        "ANALYSIS:\n- Item \ud800\n" + response(),
    ],
)
def test_analysis_does_not_relax_summary_or_unicode_validation(invalid_output):
    with pytest.raises(OutputParseError):
        parse_output(invalid_output)


def test_parse_output_accepts_catalog_vendor_scripts_and_links():
    vendor_html = (
        '<!DOCTYPE html><html lang="pt-BR"><head>'
        '<link rel="stylesheet" href="/vendor/fonts.css">'
        '<link rel="stylesheet" href="/vendor/animate-4.1.1.min.css">'
        '<script src="/vendor/tailwind.js"></script>'
        '<script src="/vendor/gsap-3.12.5.min.js"></script>'
        '<script src="/vendor/chart-4.4.7.umd.min.js"></script>'
        '<script src="/vendor/lucide-0.469.0.min.js"></script>'
        '<script src="/vendor/alpine-3.14.8.min.js" defer></script>'
        "</head><body>Painel</body></html>"
    )
    parsed = parse_output(response(vendor_html))
    assert parsed.html == vendor_html


@pytest.mark.parametrize(
    "bad_tag",
    [
        '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>',
        '<script src="//unpkg.com/gsap"></script>',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
        '<script src="/vendor/react.production.min.js"></script>',
        '<script src="/vendor/../app.js"></script>',
    ],
)
def test_parse_output_rejects_external_cdn_or_unlisted_vendor_files(bad_tag):
    bad_html = f"<!DOCTYPE html><html><head>{bad_tag}</head><body>App</body></html>"
    with pytest.raises(OutputParseError, match="/vendor/") as exc_info:
        parse_output(response(bad_html))
    assert exc_info.value.retryable is True
