"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { runInNewContext } = require("node:vm");
const readStream = runInNewContext(
  readFileSync(join(__dirname, "../web/sse.js"), "utf8") + "\nreadGenerationStream;",
  { TextDecoder },
);

function response(parts, status = 200) {
  return new Response(new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(typeof part === "string" ? new TextEncoder().encode(part) : part);
      controller.close();
    },
  }), { status, headers: { "Content-Type": "text/event-stream" } });
}

const terminal = 'event: done\ndata: {"sid":"abc","summary":"Ação concluída"}\n\n';

test("SSE accepts arbitrary byte boundaries, UTF-8, CRLF, comments and multiple data lines", async () => {
  const bytes = new TextEncoder().encode(
    ': heartbeat\r\n\r\nevent: status\r\ndata: {"step":"gerando",\r\ndata: "msg":"Ação"}\r\n\r\n' + terminal,
  );
  const parts = Array.from(bytes, (byte) => Uint8Array.of(byte));
  const events = [];
  const result = await readStream(response(parts), (name, data) => events.push([name, data]));
  assert.equal(result.summary, "Ação concluída");
  assert.equal(events[0][0], "status");
  assert.equal(events[0][1].msg, "Ação");
  assert.equal(events.length, 2);
});

test("SSE dispatches before the response finishes", async () => {
  let channel;
  const stream = new ReadableStream({ start(controller) { channel = controller; } });
  const events = [];
  const pending = readStream(new Response(stream, { headers: { "Content-Type": "text/event-stream" } }), (name) => {
    events.push(name);
    if (name === "progress") {
      channel.enqueue(new TextEncoder().encode(terminal));
      channel.close();
    }
  });
  channel.enqueue(new TextEncoder().encode('event: progress\ndata: {"chars":12}\n\n'));
  await pending;
  assert.deepEqual(events, ["progress", "done"]);
});

test("SSE error retains the safe message and offline hint, including HTTP errors", async () => {
  await assert.rejects(readStream(response(['event: error\ndata: {"msg":"Limite atingido.","hint":"Usar modo offline."}\n\n'], 422), () => {}), /Limite atingido\. Usar modo offline\./);
});

test("SSE rejects incomplete streams and incomplete terminal events", async () => {
  for (const text of ["", ': heartbeat\n\n', terminal.trimEnd(), 'event: progress\ndata: {"chars":2}\n\n']) {
    await assert.rejects(readStream(response([text]), () => {}), /antes de concluir/);
  }
});

test("SSE rejects invalid JSON, UTF-8, excessive buffers and wrong content types", async () => {
  for (const parts of [
    ['event: done\ndata: broken\n\n'],
    ['event: done\ndata: null\n\n'],
    [Uint8Array.of(0xff)],
    ["x".repeat(65537)],
  ]) await assert.rejects(readStream(response(parts), () => {}));
  await assert.rejects(readStream(new Response("{}"), () => {}), /fluxo de geração válido/);
});

test("SSE read failure propagates without reconnecting", async () => {
  const stream = new ReadableStream({ start(controller) { controller.error(new Error("connection failed")); } });
  await assert.rejects(readStream(new Response(stream, { headers: { "Content-Type": "text/event-stream" } }), () => {}), /connection failed/);
});

test("SSE accepts multiple grouped events exceeding 64 KiB total when each event fits limit", async () => {
  const chunkEvent = `event: code\ndata: {"chunk":"${"a".repeat(25000)}"}\n\n`;
  const grouped = chunkEvent.repeat(4) + terminal;
  assert.ok(grouped.length > 65536);
  let count = 0;
  const result = await readStream(response([grouped]), (name) => {
    if (name === "code") count++;
  });
  assert.equal(count, 4);
  assert.equal(result.sid, "abc");
});

test("SSE rejects a single multi-line event exceeding 64 KiB", async () => {
  const oversizedEvent = `event: code\ndata: "${"a".repeat(35000)}\ndata: ${"b".repeat(35000)}"\n\n` + terminal;
  await assert.rejects(readStream(response([oversizedEvent]), () => {}), /limite permitido/);
});

test("SSE isolates presentation errors in optional events so done is still delivered", async () => {
  const streamText =
    'event: analysis\ndata: {"items":["Botão"]}\n\n' +
    'event: code\ndata: {"chunk":"<html>"}\n\n' +
    'event: stage_timing\ndata: {"step":"gerando","ms":12}\n\n' +
    'event: usage\ndata: {"tokens_in":10}\n\n' +
    terminal;
  const seen = [];
  const result = await readStream(response([streamText]), (name) => {
    seen.push(name);
    if (["analysis", "code", "stage_timing", "usage"].includes(name)) {
      throw new Error("optional UI failure");
    }
  });
  assert.deepEqual(seen, ["analysis", "code", "stage_timing", "usage", "done"]);
  assert.equal(result.sid, "abc");
});
