"use strict";

// POST streaming via fetch: EventSource only supports GET and reconnects.
async function readGenerationStream(response, onEvent) {
  if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
    throw new Error("O servidor não enviou um fluxo de geração válido.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const optionalEvents = new Set(["analysis", "code", "stage_timing", "usage"]);
  let buffer = "", name = "message", data = [], eventSize = 0, terminal = null;

  function line(value) {
    if (!value) {
      if (data.length) {
        const payload = JSON.parse(data.join("\n"));
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Evento inválido.");
        try {
          onEvent(name, payload);
        } catch (error) {
          if (!optionalEvents.has(name)) throw error;
        }
        if (name === "done" || name === "error") terminal = { name, payload };
      }
      name = "message";
      data = [];
      eventSize = 0;
      return;
    }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "event") name = content;
    if (field === "data") data.push(content);
  }

  try {
    while (!terminal) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let match;
      while ((match = /\r\n|\r|\n/.exec(buffer))) {
        if (!done && match[0] === "\r" && match.index === buffer.length - 1) break;
        eventSize += match.index;
        if (eventSize > 65536) throw new Error("Evento excedeu o limite permitido.");
        line(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
        if (terminal) break;
      }
      if (!terminal && eventSize + buffer.length > 65536) throw new Error("Evento excedeu o limite permitido.");
      if (done && !terminal) throw new Error("A conexão terminou antes de concluir a geração. Verifique o servidor antes de tentar novamente.");
    }
    if (terminal.name === "error") {
      const { msg, hint } = terminal.payload;
      throw new Error(typeof msg === "string" ? `${msg}${typeof hint === "string" ? ` ${hint}` : ""}` : "Não foi possível gerar o app.");
    }
    if (!response.ok) throw new Error("O servidor recusou a geração.");
    return terminal.payload;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
