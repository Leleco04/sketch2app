"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { source: "camera", stream: null, cameraToken: 0, imageToken: 0,
    image: null, imageUrl: null, busy: false, processing: false, mode: "unknown",
    maxBytes: 10 * 1024 * 1024, hasResult: false, timer: null,
    sid: null, version: 0, hasSessionImage: false, isReplay: false,
    simpleMode: false, useExtras: false, useDataApi: false, timelapseRunning: false, listening: false, speechRec: null,
    prompts: null, lastDownloadedHtml: "",
    currentHtml: "", cacheItems: [], versions: [], messages: [] };

  function formatTimer(seconds) {
    const mins = String(Math.floor(seconds / 60)).padStart(2, "0");
    const secs = String(Math.max(0, seconds % 60)).padStart(2, "0");
    return `${mins}:${secs}`;
  }

  function notice(message) {
    $("notice-text").textContent = message;
    $("notice").hidden = !message;
  }

  function renderVersionBar() {
    const bar = $("version-bar");
    const list = $("version-list");
    if (!state.sid || !state.versions.length) {
      bar.hidden = true;
      list.replaceChildren();
      $("undo-version").disabled = true;
      $("redo-version").disabled = true;
      $("timelapse-button").disabled = true;
      return;
    }
    bar.hidden = false;
    list.replaceChildren(...state.versions.map((entry) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "version-pill";
      btn.dataset.version = String(entry.n);
      btn.setAttribute("aria-pressed", String(entry.n === state.version));
      btn.disabled = state.busy || state.processing || state.timelapseRunning;
      btn.textContent = `v${entry.n}`;
      btn.addEventListener("click", () => restoreVersion(entry.n));
      return btn;
    }));
    const numbers = state.versions.map((v) => v.n);
    const index = numbers.indexOf(state.version);
    $("undo-version").disabled = state.busy || state.processing || state.timelapseRunning || index <= 0;
    $("redo-version").disabled = state.busy || state.processing || state.timelapseRunning || index === -1 || index >= numbers.length - 1;
    $("timelapse-button").disabled = state.busy || state.processing || state.timelapseRunning || state.versions.length < 2;
  }

  function renderChatMessages() {
    const list = $("chat-messages");
    if (!state.messages.length) {
      const empty = document.createElement("li");
      empty.id = "chat-empty";
      empty.className = "chat-empty";
      empty.textContent = "Gere a primeira versão para pedir ajustes ou usar os atalhos abaixo.";
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...state.messages.map((entry) => {
      const item = document.createElement("li");
      item.className = `chat-message ${entry.role === "user" ? "is-user" : "is-assistant"}`;
      const role = document.createElement("strong");
      role.textContent = entry.role === "user" ? "Você" : entry.version ? `App · v${entry.version}` : "App";
      const text = document.createElement("span");
      text.textContent = entry.text;
      item.append(role, text);
      return item;
    }));
    list.scrollTop = list.scrollHeight;
  }

  function updateControls() {
    $("input-fields").disabled = state.busy;
    $("generate").disabled = state.busy || state.processing || ["offline", "unknown"].includes(state.mode)
      || (!state.image && !$("style-prompt").value.trim());
    $("capture").disabled = state.busy || state.processing
      || (!state.image && (!state.stream || $("camera-video").readyState < 2));
    const chatAvailable = Boolean(state.sid) && !state.busy && !state.processing
      && (state.isReplay || !["offline", "unknown"].includes(state.mode));
    $("chat-fields").disabled = !chatAvailable;
    for (const chip of document.querySelectorAll(".chat-chip")) chip.disabled = !chatAvailable;
    if (!state.hasSessionImage) $("reattach-image").checked = false;
    $("reattach-image").disabled = !chatAvailable || !state.hasSessionImage;
    $("reattach-hint").textContent = state.sid && !state.hasSessionImage
      ? "(sessão sem desenho)"
      : "(para corrigir interpretação)";
    $("chat-submit").disabled = !chatAvailable || !$("chat-input").value.trim();
    $("chat-status-label").textContent = state.sid ? `Sessão v${state.version}${state.isReplay ? " · replay" : ""}` : "Após gerar";
    $("cache-select").disabled = state.busy || state.processing || !state.cacheItems.length;
    $("replay-button").disabled = state.busy || state.processing || !state.cacheItems.length || !$("cache-select").value;
    $("refresh-cache").disabled = state.busy || state.processing;
    $("toggle-compare").disabled = !state.hasResult;
    $("toggle-code").disabled = !state.currentHtml;
    $("download-html").disabled = !state.sid || !state.version || state.busy;
    $("toggle-log").disabled = $("generation-log").hidden;
    $("toggle-log").setAttribute("aria-pressed", String(!$("generation-log").hidden && $("generation-log").open));
    $("toggle-fullscreen").disabled = !state.hasResult && !document.body.classList.contains("is-fullscreen");
    $("voice-button").disabled = !chatAvailable;
    renderVersionBar();
  }

  function stopCamera() {
    state.cameraToken++;
    if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
    $("camera-video").srcObject = null;
    $("camera-video").hidden = true;
    $("camera-placeholder").hidden = false;
    $("camera-select").hidden = true;
    $("start-camera").disabled = false;
    $("start-camera").textContent = "Ativar câmera ↗";
    updateControls();
  }

  function clearImage() {
    state.imageToken++;
    state.processing = false;
    if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
    state.image = null;
    state.imageUrl = null;
    $("image-preview").removeAttribute("src");
    $("selected-image").hidden = true;
    $("image-file").value = "";
    $("capture-label").textContent = "Capturar desenho";
    updateControls();
  }

  function selectSource(source) {
    if (state.busy || source === state.source) return;
    stopCamera();
    clearImage();
    state.source = source;
    for (const name of ["camera", "upload"]) {
      const selected = name === source;
      $(`${name}-tab`).setAttribute("aria-selected", String(selected));
      $(`${name}-tab`).tabIndex = selected ? 0 : -1;
      $(`${name}-panel`).hidden = !selected;
    }
    notice("");
  }

  async function startCamera(deviceId) {
    if (state.busy) return;
    stopCamera();
    clearImage();
    notice("");
    if (!navigator.mediaDevices?.getUserMedia) {
      notice("A câmera precisa de um navegador compatível em localhost ou HTTPS. Você pode enviar uma imagem.");
      return;
    }
    const token = state.cameraToken;
    $("start-camera").disabled = true;
    $("start-camera").textContent = "Aguardando câmera…";
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: {
        width: { ideal: 1280 }, height: { ideal: 960 },
        ...(deviceId ? { deviceId: { exact: deviceId } } : { facingMode: { ideal: "environment" } }),
      } });
      if (token !== state.cameraToken || state.source !== "camera") {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      state.stream = stream;
      $("camera-video").srcObject = stream;
      $("camera-video").hidden = false;
      $("camera-placeholder").hidden = true;
      await $("camera-video").play();
      if (token !== state.cameraToken) return;
      updateControls();
      try {
        const cameras = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput");
        if (token !== state.cameraToken) return;
        $("camera-select").replaceChildren(...cameras.map((camera, index) => {
          const option = document.createElement("option");
          option.value = camera.deviceId;
          option.textContent = camera.label || `Câmera ${index + 1}`;
          return option;
        }));
        $("camera-select").value = stream.getVideoTracks()[0]?.getSettings().deviceId || cameras[0]?.deviceId || "";
        $("camera-select").hidden = cameras.length < 2;
      } catch { /* Capture remains usable if device enumeration is unavailable. */ }
    } catch (error) {
      if (token !== state.cameraToken) return;
      stopCamera();
      const messages = {
        NotAllowedError: "Permissão da câmera negada. Autorize no navegador ou envie uma imagem.",
        NotFoundError: "Nenhuma câmera encontrada. Use a opção Enviar imagem.",
        NotReadableError: "A câmera está indisponível ou em uso. Feche outros apps ou envie uma imagem.",
        OverconstrainedError: "Esta câmera não está disponível. Escolha outra ou envie uma imagem.",
      };
      notice(messages[error.name] || "Não foi possível abrir a câmera. Você pode enviar uma imagem.");
    } finally {
      // The button may be used again even when an earlier permission request is pending.
      if (token === state.cameraToken || !state.stream) {
        $("start-camera").disabled = false;
        $("start-camera").textContent = "Ativar câmera ↗";
      }
      updateControls();
    }
  }

  async function useImage(blob, name, captured = false) {
    if (!blob) throw new Error("Não foi possível ler a imagem. Tente outra foto.");
    if (!["image/jpeg", "image/png", "image/webp"].includes(blob.type)) {
      throw new Error("Use uma imagem JPG, PNG ou WebP.");
    }
    if (blob.size > state.maxBytes) throw new Error("A imagem excede 10 MB. Escolha uma foto menor.");
    const token = ++state.imageToken;
    state.processing = true;
    updateControls();
    const url = URL.createObjectURL(blob);
    let accepted = false;
    try {
      const image = new Image();
      image.src = url;
      await image.decode();
      if (!image.naturalWidth || !image.naturalHeight) throw new Error("invalid_image");
      if (token !== state.imageToken) return;
      if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
      state.image = blob;
      state.imageUrl = url;
      accepted = true;
      $("image-preview").src = url;
      $("image-name").textContent = name;
      $("image-detail").textContent = `${image.naturalWidth} × ${image.naturalHeight} px · ${Math.max(1, Math.round(blob.size / 1024))} KB`;
      $("selected-image").hidden = false;
      if (captured) {
        stopCamera();
        $("capture-label").textContent = "Refazer captura";
      }
      notice("");
    } catch {
      if (token === state.imageToken) throw new Error("Não foi possível abrir essa imagem. Escolha outra foto.");
    } finally {
      if (!accepted) URL.revokeObjectURL(url);
      if (token === state.imageToken) state.processing = false;
      updateControls();
    }
  }

  async function capture() {
    if ($("capture").disabled) return;
    if (state.image) { await startCamera(); return; }
    const video = $("camera-video");
    if (!video.videoWidth || !video.videoHeight) return;
    const token = state.cameraToken;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    try {
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
      if (token !== state.cameraToken || state.busy) return;
      await useImage(blob, "Captura do desenho.jpg", true);
    } catch (error) { notice(error.message); }
  }

  async function upload(file) {
    if (!file || state.busy) return;
    try { await useImage(file, file.name); }
    catch (error) { notice(error.message); }
    finally { $("image-file").value = ""; }
  }

  function base64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(",", 2)[1]);
      reader.onerror = () => reject(new Error("Não foi possível ler o desenho. Selecione a imagem novamente."));
      reader.readAsDataURL(blob);
    });
  }

  async function readJson(response) {
    try { return await response.json(); }
    catch { throw new Error("O servidor respondeu em um formato inesperado. Verifique se ele está funcionando."); }
  }

  async function showApp(result, signal) {
    if (!/^[0-9a-f]{32}$/.test(result.sid) || !Number.isInteger(result.version) || result.version < 1
      || result.url !== `/app/${result.sid}/${result.version}` || typeof result.summary !== "string") {
      throw new Error("O servidor retornou uma prévia inválida. Tente novamente.");
    }
    const response = await fetch(result.url, { signal, cache: "no-store" });
    if (!response.ok || !response.headers.get("content-type")?.includes("text/html")) {
      throw new Error("O app foi gerado, mas não foi possível carregar a prévia. Tente novamente.");
    }
    const html = await response.text();
    if (new TextEncoder().encode(html).length >= 300 * 1024) throw new Error("A prévia excedeu o tamanho permitido.");
    const frame = document.createElement("iframe");
    frame.title = "App gerado a partir da sua ideia";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("allow", "gamepad");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.hidden = true;
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => finish(new Error("A prévia demorou para abrir. Tente novamente.")), 10000);
        const abort = () => finish(new DOMException("Aborted", "AbortError"));
        function finish(error) {
          clearTimeout(timer);
          signal.removeEventListener("abort", abort);
          frame.onload = null;
          error ? reject(error) : resolve();
        }
        frame.onload = () => finish();
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) { abort(); return; }
        frame.srcdoc = html;
        $("frame-host").append(frame);
      });
      // Keep the loaded frame in place: moving it recreates its browsing context.
      for (const previous of Array.from($("frame-host").children)) {
        if (previous !== frame) previous.remove();
      }
      frame.hidden = false;
      $("frame-host").hidden = false;
      $("empty-preview").hidden = true;
      $("version-badge").textContent = `v${result.version}`;
      $("version-badge").hidden = false;
      $("result-summary").textContent = result.summary;
      $("result-summary").hidden = false;
      state.currentHtml = html;
      const lines = html.split("\n").length;
      const kb = Math.max(1, Math.round(new TextEncoder().encode(html).length / 1024));
      $("code-panel-title").textContent = `Código HTML da versão v${result.version}`;
      $("code-meta").textContent = `${lines} linhas · ${kb} KB`;
      $("code-content").textContent = html;
      state.hasResult = true;
    } catch (error) { frame.remove(); throw error; }
  }

  const PIPE_ORDER = ["foto", "preparando", "gerando", "validando", "gravando"];

  function setPipelineStep(activeStep) {
    const mapped = activeStep === "enviando" ? "gerando" : activeStep;
    const activeIndex = PIPE_ORDER.indexOf(mapped);
    if (activeIndex === -1) return;
    for (const item of document.querySelectorAll("#stage-pipeline li")) {
      const idx = PIPE_ORDER.indexOf(item.dataset.pipe);
      item.classList.toggle("is-done", idx < activeIndex);
      item.classList.toggle("is-active", idx === activeIndex);
    }
  }

  function resetStageScreen(hasDrawing, drawingUrl) {
    for (const item of document.querySelectorAll("#stage-pipeline li")) {
      item.classList.toggle("is-done", item.dataset.pipe === "foto");
      item.classList.toggle("is-active", item.dataset.pipe === "preparando");
    }
    for (const badge of document.querySelectorAll("#stage-pipeline small[data-pipe-ms]")) {
      badge.textContent = "";
    }
    const img = $("stage-drawing-img");
    const fallback = $("stage-text-fallback");
    const scanLine = document.querySelector("#stage-drawing-box .scan-line");
    if (hasDrawing && drawingUrl) {
      img.src = drawingUrl;
      img.hidden = false;
      fallback.hidden = true;
      if (scanLine) scanLine.hidden = false;
    } else {
      img.removeAttribute("src");
      img.hidden = true;
      fallback.hidden = false;
      if (scanLine) scanLine.hidden = true;
    }
    $("stage-analysis-list").replaceChildren();
    $("stage-analysis-empty").hidden = true;
    $("stage-stream-code").textContent = "";
    $("stage-tokens-badge").textContent = "~0 tokens (estimado)";
  }

  function updateBackstageAndCard({ telemetry, totalSeconds, hasDrawing, isChat }) {
    const lastUsage = telemetry.usages[telemetry.usages.length - 1] || null;
    $("card-total-time").textContent = `${totalSeconds.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} s`;
    if (lastUsage && (lastUsage.tokens_in !== null || lastUsage.tokens_out !== null)) {
      const tIn = lastUsage.tokens_in !== null && lastUsage.tokens_in !== undefined ? lastUsage.tokens_in.toLocaleString("pt-BR") : "—";
      const tOut = lastUsage.tokens_out !== null && lastUsage.tokens_out !== undefined ? lastUsage.tokens_out.toLocaleString("pt-BR") : "—";
      $("card-tokens").textContent = `${tIn} in / ${tOut} out`;
      const think = lastUsage.tokens_thinking ? ` · thinking ${lastUsage.tokens_thinking}` : "";
      $("backstage-usage").textContent = `Modelo: ${lastUsage.model || state.mode} · Tokens: ${tIn} entrada / ${tOut} saída${think} · Latência: ${lastUsage.latency_ms ?? 0} ms`;
    } else {
      $("card-tokens").textContent = "indisponível";
      $("backstage-usage").textContent = "Métricas de uso indisponíveis nesta operação.";
    }
    $("card-elements").textContent = telemetry.analysis.length
      ? `${telemetry.analysis.length} itens`
      : (hasDrawing ? "análise indisponível" : "somente texto");
    $("card-model").textContent = lastUsage?.model || state.mode;
    $("stage-summary-card").hidden = state.simpleMode || !state.hasResult;
    if (state.prompts) {
      const basePrompt = isChat
        ? state.prompts.chat
        : (hasDrawing ? state.prompts.generate : state.prompts.generate_text);
      $("backstage-prompt").textContent = state.useExtras && state.prompts.extras
        ? `${basePrompt}\n\n${state.prompts.extras}`
        : basePrompt;
    }
  }

  async function runStreamOperation({ url, buildPayload, initialStep, isChat, isReplay = false, hasDrawing = false, drawingUrl = null, onSuccess }) {
    state.busy = true;
    stopCamera();
    toggleComparePanel(false);
    updateControls();
    notice("");
    const started = performance.now();
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 240000);
    document.body.classList.add("is-generating");
    if (isReplay) {
      $("replay-label").textContent = "Reproduzindo…";
    } else if (isChat) {
      $("chat-submit-label").textContent = "Aplicando ajuste…";
    } else {
      $("generate-label").textContent = "Criando seu app…";
    }
    resetStageScreen(hasDrawing, drawingUrl);
    $("loading-overlay").hidden = false;
    $("preview-section").setAttribute("aria-busy", "true");
    $("status-text").textContent = initialStep;
    $("status-dot").className = "busy";
    $("elapsed").textContent = "00:00";
    $("generation-step").textContent = initialStep;
    $("generation-chars").textContent = "0 caracteres recebidos";
    $("generation-log").hidden = false;
    $("generation-events").replaceChildren();
    state.timer = setInterval(() => {
      $("elapsed").textContent = formatTimer(Math.floor((performance.now() - started) / 1000));
    }, 1000);
    try {
      const payload = await buildPayload();
      $("status-text").textContent = isReplay ? "Preparando gravação offline…" : state.mode === "mock" ? "Preparando uma prévia simulada…" : "Aguardando a geração do app…";
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: payload !== undefined ? JSON.stringify(payload) : undefined,
        signal: controller.signal,
      });
      const telemetry = { attempt: 1, analysis: [], code: "", estTokens: 0, timings: [], usages: [] };
      state.lastGeneration = telemetry;
      function syncAttempt(attempt) {
        if (Number.isInteger(attempt) && attempt > telemetry.attempt) {
          telemetry.attempt = attempt;
          telemetry.analysis = [];
          telemetry.code = "";
          telemetry.estTokens = 0;
          $("generation-chars").textContent = "0 caracteres recebidos";
          $("stage-analysis-list").replaceChildren();
          $("stage-analysis-empty").hidden = true;
          $("stage-stream-code").textContent = "";
          $("stage-tokens-badge").textContent = "~0 tokens (estimado)";
        }
      }
      const result = await readGenerationStream(response, (name, data) => {
        if (name === "status") {
          if (!["preparando", "enviando", "gerando", "validando", "gravando"].includes(data.step) || typeof data.msg !== "string") {
            throw new Error("Etapa de geração inválida.");
          }
          syncAttempt(data.attempt);
          setPipelineStep(data.step);
          $("status-text").textContent = data.msg;
          $("generation-step").textContent = data.msg;
          if (data.step === "gerando") {
            telemetry.analysis = [];
            telemetry.code = "";
            telemetry.estTokens = 0;
            $("generation-chars").textContent = "0 caracteres recebidos";
            $("stage-analysis-list").replaceChildren();
            $("stage-analysis-empty").hidden = true;
            $("stage-stream-code").textContent = "";
          } else if (data.step === "validando" && !telemetry.analysis.length) {
            $("stage-analysis-empty").hidden = false;
          }
          const item = document.createElement("li");
          item.textContent = data.msg;
          $("generation-events").append(item);
        } else if (name === "progress") {
          if (!Number.isSafeInteger(data.chars) || data.chars < 0) throw new Error("Progresso de geração inválido.");
          syncAttempt(data.attempt);
          const est = Number.isSafeInteger(data.est_tokens) && data.est_tokens >= 0 ? data.est_tokens : Math.floor(data.chars / 4);
          telemetry.estTokens = est;
          const suffix = isReplay ? " · replay" : state.mode === "mock" ? " · simulação" : "";
          $("generation-chars").textContent = `${data.chars.toLocaleString("pt-BR")} caracteres recebidos · ~${est.toLocaleString("pt-BR")} tokens (estimado)${suffix}`;
          $("stage-tokens-badge").textContent = `~${est.toLocaleString("pt-BR")} tokens (estimado)`;
        } else {
          try {
            syncAttempt(data.attempt);
            if (name === "analysis" && Array.isArray(data.items)) {
              telemetry.analysis = data.items.filter((entry) => typeof entry === "string");
              $("stage-analysis-list").replaceChildren(...telemetry.analysis.map((entry) => {
                const li = document.createElement("li");
                li.textContent = entry;
                return li;
              }));
              $("stage-analysis-empty").hidden = telemetry.analysis.length > 0;
              if (telemetry.analysis.length) {
                const item = document.createElement("li");
                item.textContent = `Leitura do desenho: ${telemetry.analysis.join(" · ")}`;
                $("generation-events").append(item);
              }
            } else if (name === "code" && typeof data.chunk === "string") {
              // Partial HTML stays strictly in plain text; never injected into the DOM or iframe.
              telemetry.code += data.chunk;
              const codeBox = $("stage-stream-code");
              codeBox.textContent = telemetry.code;
              codeBox.scrollTop = codeBox.scrollHeight;
            } else if (name === "stage_timing" && typeof data.step === "string" && Number.isFinite(data.ms) && data.ms >= 0) {
              telemetry.timings.push({ step: data.step, ms: data.ms, wait_ms: data.wait_ms || 0, attempt: data.attempt || telemetry.attempt });
              const pipeKey = data.step === "enviando" ? "gerando" : data.step;
              const badge = document.querySelector(`#stage-pipeline small[data-pipe-ms="${pipeKey}"]`);
              if (badge) badge.textContent = `${data.ms} ms`;
            } else if (name === "usage" && data && typeof data === "object") {
              telemetry.usages.push(data);
              if (data.tokens_out !== null && data.tokens_out !== undefined) {
                const tIn = data.tokens_in !== null && data.tokens_in !== undefined ? `${data.tokens_in.toLocaleString("pt-BR")} in / ` : "";
                $("stage-tokens-badge").textContent = `${tIn}${data.tokens_out.toLocaleString("pt-BR")} out tokens`;
              }
            }
          } catch { /* Optional event presentation failures never interrupt generation. */ }
        }
      });
      await showApp(result, controller.signal);
      await onSuccess(result, payload);
      for (const item of document.querySelectorAll("#stage-pipeline li")) {
        item.classList.add("is-done");
        item.classList.remove("is-active");
      }
      if ((isReplay || state.isReplay) && !telemetry.usages.length) {
        const item = document.createElement("li");
        item.textContent = "Métricas da geração: informação indisponível na gravação.";
        $("generation-events").append(item);
      }
      const totalSeconds = (performance.now() - started) / 1000;
      updateBackstageAndCard({
        telemetry,
        totalSeconds,
        hasDrawing: hasDrawing || state.hasSessionImage,
        isChat,
      });
      $("status-text").textContent = "Pronto para experimentar";
      const modeTag = isReplay || state.isReplay
        ? (telemetry.usages.length ? "replay" : "replay · métricas indisponíveis")
        : state.mode === "mock" ? "simulação" : isChat ? "ajuste" : "geração";
      $("generation-time").textContent = `${totalSeconds.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} s · ${modeTag}`;
    } catch (error) {
      notice(error.name === "AbortError" ? "A geração excedeu o tempo de espera. Verifique o servidor antes de tentar novamente."
        : error instanceof TypeError ? "Não foi possível conectar ao servidor local. Verifique se ele está funcionando."
        : error.message);
      $("status-text").textContent = state.hasResult ? "Última prévia preservada" : "Pronto para uma nova tentativa";
    } finally {
      clearTimeout(deadline);
      clearInterval(state.timer);
      state.busy = false;
      document.body.classList.remove("is-generating");
      $("generate-label").textContent = "Gerar meu app";
      $("chat-submit-label").textContent = "Enviar ajuste";
      $("replay-label").textContent = "Reproduzir gravação";
      $("loading-overlay").hidden = true;
      $("preview-section").setAttribute("aria-busy", "false");
      $("status-dot").className = state.hasResult ? "ready" : "";
      updateControls();
    }
  }

  async function generate(event) {
    event.preventDefault();
    if ($("generate").disabled || state.busy) return;
    const sentImage = state.image;
    const sentImageUrl = state.imageUrl;
    const sentPrompt = $("style-prompt").value.trim();
    await runStreamOperation({
      url: "/api/sessions",
      initialStep: "Preparando seu desenho e suas instruções…",
      isChat: false,
      hasDrawing: Boolean(sentImage),
      drawingUrl: sentImageUrl,
      buildPayload: async () => {
        const payload = { prompt: sentPrompt };
        if (sentImage) payload.image_b64 = await base64(sentImage);
        if (state.useExtras) payload.use_extras = true;
        if (state.useDataApi) payload.use_data_api = true;
        return payload;
      },
      onSuccess: (result) => {
        state.sid = result.sid;
        state.version = result.version;
        state.isReplay = false;
        state.hasSessionImage = Boolean(sentImage);
        state.versions = [{ n: result.version, summary: result.summary }];
        $("reattach-image").checked = false;
        state.messages = [
          { role: "user", text: sentPrompt || "Desenho enviado." },
          { role: "assistant", text: result.summary, version: result.version },
        ];
        renderChatMessages();
        loadCacheList();
      },
    });
  }

  async function sendChatMessage(rawText, fromInput = false) {
    const text = (rawText || "").trim();
    if (!text || !state.sid || state.busy || (!state.isReplay && ["offline", "unknown"].includes(state.mode))) return;
    const reattach = Boolean(state.hasSessionImage && $("reattach-image").checked);
    await runStreamOperation({
      url: `/api/sessions/${state.sid}/messages`,
      initialStep: "Preparando o contexto da sessão e seu pedido…",
      isChat: true,
      hasDrawing: state.hasSessionImage,
      drawingUrl: state.hasSessionImage ? `/api/sessions/${state.sid}/image` : null,
      buildPayload: async () => {
        const payload = { text, reattach_image: reattach };
        if (state.useExtras) payload.use_extras = true;
        if (state.useDataApi) payload.use_data_api = true;
        return payload;
      },
      onSuccess: (result) => {
        state.version = result.version;
        if (!state.versions.some((v) => v.n === result.version)) {
          state.versions.push({ n: result.version, summary: result.summary });
        }
        state.messages.push(
          { role: "user", text },
          { role: "assistant", text: result.summary, version: result.version },
        );
        if (fromInput) $("chat-input").value = "";
        $("reattach-image").checked = false;
        renderChatMessages();
      },
    });
  }

  async function loadCacheList() {
    try {
      const response = await fetch("/api/cache", { cache: "no-store", signal: AbortSignal.timeout(10000) });
      const data = await readJson(response);
      if (!response.ok || !Array.isArray(data?.items)) throw new Error("cache");
      state.cacheItems = data.items;
      const select = $("cache-select");
      const current = select.value;
      if (!state.cacheItems.length) {
        const emptyOpt = document.createElement("option");
        emptyOpt.value = "";
        emptyOpt.textContent = "Nenhuma gravação disponível em cache/";
        select.replaceChildren(emptyOpt);
        $("cache-status").textContent = "Nenhuma gravação encontrada em cache/. Grave sessões com RECORD=true.";
      } else {
        select.replaceChildren(...state.cacheItems.map((item) => {
          const opt = document.createElement("option");
          opt.value = item.name;
          const label = item.prompt || item.summary || item.name;
          opt.textContent = `${item.name} — ${label.slice(0, 60)} (${item.versions} v)`;
          return opt;
        }));
        if (!$("offline-panel").hidden && state.cacheItems.some((item) => item.name === current)) {
          select.value = current;
        }
        $("cache-status").textContent = `${state.cacheItems.length} gravação(ões) pronta(s) para reprodução sem API.`;
      }
    } catch {
      $("cache-status").textContent = "Não foi possível carregar a lista de gravações do cache.";
    }
    updateControls();
  }

  function toggleOfflinePanel(forceOpen) {
    const panel = $("offline-panel");
    const open = typeof forceOpen === "boolean" ? forceOpen : panel.hidden;
    panel.hidden = !open;
    $("toggle-offline-panel").setAttribute("aria-expanded", String(open));
    if (open) loadCacheList();
  }

  async function replayCachedSession() {
    const name = $("cache-select").value;
    if (!name || state.busy || state.processing) return;
    await runStreamOperation({
      url: `/api/replay/${encodeURIComponent(name)}`,
      initialStep: "Preparando gravação offline do cache…",
      isChat: false,
      isReplay: true,
      buildPayload: async () => undefined,
      onSuccess: async (result) => {
        state.sid = result.sid;
        state.version = result.version;
        state.isReplay = true;
        $("reattach-image").checked = false;
        try {
          const detailResp = await fetch(`/api/sessions/${result.sid}`, { cache: "no-store" });
          const detail = await readJson(detailResp);
          state.hasSessionImage = Boolean(detail.has_image);
          state.versions = detail.versions.map((v) => ({ n: v.n, summary: v.summary }));
          state.messages = detail.messages.map((m, idx) => ({
            role: m.role,
            text: m.text,
            version: m.role === "assistant" ? Math.floor(idx / 2) + 1 : undefined,
          }));
        } catch {
          state.hasSessionImage = false;
          state.versions = [{ n: result.version, summary: result.summary }];
          state.messages = [
            { role: "user", text: "Sessão gravada." },
            { role: "assistant", text: result.summary, version: result.version },
          ];
        }
        renderChatMessages();
      },
    });
  }

  async function restoreVersion(targetVersion) {
    if (!state.sid || state.busy || state.processing || targetVersion === state.version) return;
    state.busy = true;
    updateControls();
    notice("");
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/sessions/${state.sid}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: targetVersion }),
        signal: controller.signal,
      });
      const result = await readJson(response);
      if (!response.ok) {
        const message = typeof result?.msg === "string" && result.msg.trim()
          ? result.msg
          : "Não foi possível restaurar a versão.";
        const hint = typeof result?.hint === "string" && result.hint.trim() ? ` ${result.hint}` : "";
        throw new Error(message + hint);
      }
      await showApp(result, controller.signal);
      state.version = result.version;
      $("status-text").textContent = `Versão v${result.version} ativa`;
      $("generation-time").textContent = "Troca sem chamada à IA";
    } catch (error) {
      notice(error.name === "AbortError" ? "A restauração da versão demorou além do esperado."
        : error instanceof TypeError ? "Não foi possível conectar ao servidor local."
        : error.message);
    } finally {
      clearTimeout(deadline);
      state.busy = false;
      $("status-dot").className = state.hasResult ? "ready" : "";
      updateControls();
    }
  }

  async function loadConfig() {
    try {
      const response = await fetch("/api/config", { cache: "no-store", signal: AbortSignal.timeout(10000) });
      const config = await readJson(response);
      if (!response.ok || !["mock", "live", "offline"].includes(config.mode)) throw new Error("config");
      state.mode = config.mode;
      if (Number.isInteger(config.max_image_bytes) && config.max_image_bytes > 0) state.maxBytes = config.max_image_bytes;
      const labels = { mock: "Simulação ativa", live: "Modo Gemini", offline: "Modo offline" };
      $("mode-badge").replaceChildren(Object.assign(document.createElement("i"), { ariaHidden: "true" }), document.createTextNode(labels[state.mode]));
      $("mode-badge").dataset.mode = state.mode;
      $("mode-note").textContent = state.mode === "mock"
        ? "Um ensaio sem gastar cota. A prévia será simulada; seu desenho e suas instruções ficam salvos localmente."
        : state.mode === "offline" ? "O servidor está em modo offline; a geração de novos apps está indisponível. Use o painel abaixo para reproduzir uma gravação." : "";
      $("mode-note").hidden = state.mode === "live";
      if (state.mode === "offline") toggleOfflinePanel(true);
    } catch {
      $("mode-badge").textContent = "Modo não verificado";
      notice("Não foi possível verificar o modo do servidor. Reinicie-o caso tenha acabado de atualizar os arquivos.");
    }
    updateControls();
  }

  for (const name of ["camera", "upload"]) {
    $(`${name}-tab`).addEventListener("click", () => selectSource(name));
    $(`${name}-tab`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? "camera" : event.key === "End" ? "upload" : name === "camera" ? "upload" : "camera";
      selectSource(target);
      $(`${target}-tab`).focus();
    });
  }
  $("start-camera").addEventListener("click", () => startCamera());
  $("camera-select").addEventListener("change", () => startCamera($("camera-select").value));
  $("camera-video").addEventListener("loadeddata", updateControls);
  $("capture").addEventListener("click", capture);
  $("remove-image").addEventListener("click", clearImage);
  $("upload-zone").addEventListener("click", () => $("image-file").click());
  $("image-file").addEventListener("change", () => upload($("image-file").files[0]));
  for (const type of ["dragenter", "dragover", "dragleave", "drop"]) {
    $("upload-zone").addEventListener(type, (event) => {
      event.preventDefault();
      if (state.busy) return;
      $("upload-zone").classList.toggle("is-dragging", type === "dragenter" || type === "dragover");
      if (type === "drop") upload(event.dataTransfer.files[0]);
    });
  }
  // Prevent the browser from navigating away when a file misses the drop area.
  for (const type of ["dragover", "drop"]) window.addEventListener(type, (event) => {
    if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
  });
  $("style-prompt").addEventListener("input", updateControls);
  $("generation-form").addEventListener("submit", generate);
  $("chat-input").addEventListener("input", updateControls);
  $("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      if (!$("chat-submit").disabled) $("chat-form").requestSubmit();
    }
  });
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendChatMessage($("chat-input").value.trim(), true);
  });
  for (const chip of document.querySelectorAll(".chat-chip")) {
    chip.addEventListener("click", () => {
      if (!chip.disabled) sendChatMessage(chip.dataset.chip, false);
    });
  }
  function toggleExtrasMode(forceExtras) {
    state.useExtras = typeof forceExtras === "boolean" ? forceExtras : !state.useExtras;
    $("toggle-extras-mode").setAttribute("aria-pressed", String(state.useExtras));
  }

  function toggleDataApi(forceDataApi) {
    state.useDataApi = typeof forceDataApi === "boolean" ? forceDataApi : !state.useDataApi;
    $("toggle-data-api")?.setAttribute("aria-pressed", String(state.useDataApi));
  }

  function toggleSimpleMode(forceSimple) {
    state.simpleMode = typeof forceSimple === "boolean" ? forceSimple : !state.simpleMode;
    document.body.classList.toggle("is-simple-mode", state.simpleMode);
    $("toggle-simple-mode").setAttribute("aria-pressed", String(state.simpleMode));
    $("stage-summary-card").hidden = state.simpleMode || !state.hasResult;
    if (state.simpleMode) toggleComparePanel(false);
  }

  function toggleComparePanel(forceOpen) {
    if (!state.hasResult && forceOpen !== false) return;
    const panel = $("compare-panel");
    const open = typeof forceOpen === "boolean" ? forceOpen : panel.hidden;
    panel.hidden = !open;
    $("toggle-compare").setAttribute("aria-pressed", String(open));
    if (open) {
      const img = $("compare-image");
      const noImg = $("compare-no-image");
      if (state.hasSessionImage && state.sid) {
        img.src = `/api/sessions/${state.sid}/image`;
        img.hidden = false;
        noImg.hidden = true;
      } else {
        img.removeAttribute("src");
        img.hidden = true;
        noImg.hidden = false;
      }
      $("compare-before").style.setProperty("--compare-split", `${$("compare-slider").value}%`);
    }
  }

  async function runTimelapse() {
    if (!state.sid || state.versions.length < 2 || state.busy || state.processing || state.timelapseRunning) return;
    state.timelapseRunning = true;
    $("timelapse-button").textContent = "⏳ Timelapse…";
    updateControls();
    try {
      const numbers = state.versions.map((v) => v.n);
      for (const n of numbers) {
        if (n !== state.version) {
          await restoreVersion(n);
        }
        await new Promise((resolve) => setTimeout(resolve, 260));
      }
      $("status-text").textContent = `Timelapse concluído (${numbers.length} versões, 0 chamadas à IA)`;
    } finally {
      state.timelapseRunning = false;
      $("timelapse-button").textContent = "▶ Timelapse";
      updateControls();
    }
  }

  async function downloadStandaloneHtml() {
    if (!state.sid || !state.version || state.busy) return;
    try {
      const response = await fetch(`/api/sessions/${state.sid}/download/${state.version}`, { cache: "no-store" });
      if (!response.ok) throw new Error("download");
      const standalone = await response.text();
      state.lastDownloadedHtml = standalone;
      window.lastDownloadedHtml = standalone;
      const blob = new Blob([standalone], { type: "text/html;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `sketch2app-${state.sid}-v${state.version}.html`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      $("status-text").textContent = `HTML autônomo v${state.version} exportado`;
    } catch {
      notice("Não foi possível exportar o HTML autônomo desta versão.");
    }
  }

  function toggleVoiceInput() {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) {
      notice("Ditado por voz não está disponível neste navegador. Você pode digitar normalmente no chat.");
      return;
    }
    if (state.listening && state.speechRec) {
      try { state.speechRec.stop(); } catch { /* ignore */ }
      state.listening = false;
      $("voice-button").setAttribute("aria-pressed", "false");
      $("voice-button").textContent = "🎤 Voz";
      return;
    }
    try {
      const rec = new SpeechRec();
      rec.lang = "pt-BR";
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      state.speechRec = rec;
      state.listening = true;
      $("voice-button").setAttribute("aria-pressed", "true");
      $("voice-button").textContent = "⏹ Ouvindo…";
      rec.onresult = (event) => {
        const transcript = event.results?.[0]?.[0]?.transcript?.trim();
        if (transcript) {
          const input = $("chat-input");
          input.value = input.value.trim() ? `${input.value.trim()} ${transcript}` : transcript;
          input.dispatchEvent(new Event("input"));
        }
      };
      rec.onerror = () => {
        notice("Não foi possível capturar o áudio do microfone. Continue digitando no chat.");
      };
      rec.onend = () => {
        state.listening = false;
        $("voice-button").setAttribute("aria-pressed", "false");
        $("voice-button").textContent = "🎤 Voz";
      };
      rec.start();
    } catch {
      state.listening = false;
      $("voice-button").setAttribute("aria-pressed", "false");
      $("voice-button").textContent = "🎤 Voz";
      notice("Não foi possível iniciar o reconhecimento de voz.");
    }
  }

  async function loadPrompts() {
    try {
      const response = await fetch("/api/prompts", { cache: "no-store" });
      if (response.ok) state.prompts = await response.json();
    } catch { /* Optional backstage prompts */ }
  }

  function toggleCodePanel(forceOpen) {
    if (!state.currentHtml && forceOpen !== false) return;
    const panel = $("code-panel");
    const open = typeof forceOpen === "boolean" ? forceOpen : panel.hidden;
    panel.hidden = !open;
    $("toggle-code").setAttribute("aria-pressed", String(open));
  }

  function toggleLogPanel(forceOpen) {
    const details = $("generation-log");
    if (details.hidden) return;
    details.open = typeof forceOpen === "boolean" ? forceOpen : !details.open;
    $("toggle-log").setAttribute("aria-pressed", String(details.open));
  }

  function toggleFullscreen(forceFull) {
    if (!state.hasResult && forceFull !== false && !document.body.classList.contains("is-fullscreen")) return;
    const active = typeof forceFull === "boolean" ? forceFull : !document.body.classList.contains("is-fullscreen");
    document.body.classList.toggle("is-fullscreen", active);
    $("toggle-fullscreen").setAttribute("aria-pressed", String(active));
    $("toggle-fullscreen").textContent = active ? "Sair da tela cheia" : "Tela cheia";
    updateControls();
  }

  async function copyCurrentCode() {
    if (!state.currentHtml) return;
    try {
      await navigator.clipboard.writeText(state.currentHtml);
      $("copy-code").textContent = "Copiado!";
      setTimeout(() => { $("copy-code").textContent = "Copiar HTML"; }, 1500);
    } catch {
      notice("Não foi possível copiar automaticamente. Selecione o texto no painel de código.");
    }
  }

  $("undo-version").addEventListener("click", () => {
    const numbers = state.versions.map((v) => v.n);
    const index = numbers.indexOf(state.version);
    if (index > 0) restoreVersion(numbers[index - 1]);
  });
  $("redo-version").addEventListener("click", () => {
    const numbers = state.versions.map((v) => v.n);
    const index = numbers.indexOf(state.version);
    if (index !== -1 && index < numbers.length - 1) restoreVersion(numbers[index + 1]);
  });
  $("timelapse-button").addEventListener("click", () => runTimelapse());
  $("toggle-data-api")?.addEventListener("click", () => toggleDataApi());
  $("toggle-extras-mode").addEventListener("click", () => toggleExtrasMode());
  $("toggle-simple-mode").addEventListener("click", () => toggleSimpleMode());
  $("toggle-compare").addEventListener("click", () => toggleComparePanel());
  $("card-compare-btn").addEventListener("click", () => toggleComparePanel(true));
  $("close-compare").addEventListener("click", () => toggleComparePanel(false));
  $("compare-slider").addEventListener("input", () => {
    $("compare-before").style.setProperty("--compare-split", `${$("compare-slider").value}%`);
  });
  $("download-html").addEventListener("click", () => downloadStandaloneHtml());
  $("voice-button").addEventListener("click", () => toggleVoiceInput());
  $("toggle-code").addEventListener("click", () => toggleCodePanel());
  $("close-code").addEventListener("click", () => toggleCodePanel(false));
  $("copy-code").addEventListener("click", () => copyCurrentCode());
  $("toggle-log").addEventListener("click", () => toggleLogPanel());
  $("generation-log").addEventListener("toggle", () => {
    $("toggle-log").setAttribute("aria-pressed", String(!$("generation-log").hidden && $("generation-log").open));
  });
  $("toggle-fullscreen").addEventListener("click", () => toggleFullscreen());
  $("toggle-offline-panel").addEventListener("click", () => toggleOfflinePanel());
  $("refresh-cache").addEventListener("click", () => loadCacheList());
  $("cache-select").addEventListener("change", updateControls);
  $("replay-button").addEventListener("click", () => replayCachedSession());

  async function loadAudienceQueue() {
    const statusEl = $("audience-status");
    const listEl = $("audience-queue-list");
    const qrSvgEl = $("audience-qr-svg");
    const urlEl = $("audience-public-url");
    try {
      const resp = await fetch("/api/queue", { cache: "no-store" });
      if (!resp.ok) throw new Error("queue_error");
      const data = await resp.json();
      if (data.qr_svg && data.public_url) {
        qrSvgEl.innerHTML = data.qr_svg;
        urlEl.textContent = data.public_url;
      } else {
        qrSvgEl.replaceChildren();
        urlEl.textContent = "Defina PUBLIC_BASE_URL no .env com a URL do túnel para exibir o QR Code.";
      }
      const items = Array.isArray(data.items) ? data.items : [];
      statusEl.textContent = items.length
        ? `${items.length} desenho(s) enviado(s) pela plateia aguardando aprovação.`
        : "Nenhum desenho pendente na fila de aprovação.";
      if (!items.length) {
        const emptyP = document.createElement("p");
        emptyP.className = "field-help";
        emptyP.textContent = "Nenhum desenho aguardando aprovação.";
        listEl.replaceChildren(emptyP);
        return;
      }
      listEl.replaceChildren(...items.map((item) => {
        const card = document.createElement("article");
        card.className = "audience-item";
        card.dataset.qid = item.qid;

        const img = document.createElement("img");
        img.src = `/api/queue/${item.qid}/image`;
        img.alt = `Desenho enviado por ${item.author || "plateia"}`;

        const meta = document.createElement("div");
        meta.className = "audience-item-meta";
        const strong = document.createElement("strong");
        strong.textContent = item.author || "Participante da plateia";
        const span = document.createElement("span");
        span.textContent = item.note || "Sem observação de estilo";
        meta.append(strong, span);

        const actions = document.createElement("div");
        actions.className = "audience-item-actions";
        const approveBtn = document.createElement("button");
        approveBtn.type = "button";
        approveBtn.className = "button button-light audience-approve";
        approveBtn.textContent = "Aprovar";
        approveBtn.disabled = state.busy;
        approveBtn.addEventListener("click", async () => {
          if (state.busy) return;
          approveBtn.disabled = true;
          try {
            const imgRes = await fetch(`/api/queue/${item.qid}/image`, { cache: "no-store" });
            if (!imgRes.ok) throw new Error("Não foi possível carregar o desenho da fila.");
            const rawBlob = await imgRes.blob();
            const jpegBlob = new Blob([rawBlob], { type: "image/jpeg" });
            stopCamera();
            await useImage(jpegBlob, `plateia-${item.author || item.qid.slice(0, 6)}.jpg`);
            if (item.note) {
              $("style-prompt").value = item.note;
            }
            await fetch(`/api/queue/${item.qid}`, { method: "DELETE" });
            await loadAudienceQueue();
            updateControls();
            $("generate").focus();
          } catch (err) {
            notice(err.message || "Falha ao aprovar desenho da plateia.");
            approveBtn.disabled = false;
          }
        });

        const rejectBtn = document.createElement("button");
        rejectBtn.type = "button";
        rejectBtn.className = "button button-light audience-reject";
        rejectBtn.textContent = "Descartar";
        rejectBtn.disabled = state.busy;
        rejectBtn.addEventListener("click", async () => {
          rejectBtn.disabled = true;
          try {
            await fetch(`/api/queue/${item.qid}`, { method: "DELETE" });
            await loadAudienceQueue();
          } catch {
            rejectBtn.disabled = false;
          }
        });

        actions.append(approveBtn, rejectBtn);
        card.append(img, meta, actions);
        return card;
      }));
    } catch {
      statusEl.textContent = "Não foi possível consultar a fila da plateia.";
    }
  }

  function toggleAudiencePanel(forceOpen) {
    const panel = $("audience-panel");
    const btn = $("toggle-audience-panel");
    if (!panel || !btn) return;
    const nextOpen = typeof forceOpen === "boolean" ? forceOpen : panel.hidden;
    panel.hidden = !nextOpen;
    btn.setAttribute("aria-expanded", String(nextOpen));
    if (nextOpen) {
      loadAudienceQueue();
    }
  }

  $("toggle-audience-panel")?.addEventListener("click", () => toggleAudiencePanel());
  $("refresh-audience")?.addEventListener("click", () => loadAudienceQueue());
  $("dismiss-notice").addEventListener("click", () => notice(""));
  document.addEventListener("keydown", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (event.key === "Escape") {
      if (!$("code-panel").hidden) {
        event.preventDefault();
        toggleCodePanel(false);
        return;
      }
      if (!$("compare-panel").hidden) {
        event.preventDefault();
        toggleComparePanel(false);
        return;
      }
      if (document.body.classList.contains("is-fullscreen")) {
        event.preventDefault();
        toggleFullscreen(false);
        return;
      }
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !state.busy) {
      event.preventDefault();
      if (target?.closest("#chat-form") && !$("chat-submit").disabled) {
        $("chat-form").requestSubmit();
      } else {
        $("generation-form").requestSubmit();
      }
      return;
    }
    if (event.repeat || event.ctrlKey || event.metaKey || event.altKey
      || target?.closest("input,textarea,select,[contenteditable]")) {
      return;
    }
    if (event.code === "Space" && !target?.closest("button,a")
      && state.source === "camera" && state.stream && !$("capture").disabled) {
      event.preventDefault();
      capture();
    } else if (event.key.toLowerCase() === "f" && !$("toggle-fullscreen").disabled) {
      event.preventDefault();
      toggleFullscreen();
    } else if (event.key.toLowerCase() === "c" && !$("toggle-code").disabled) {
      event.preventDefault();
      toggleCodePanel();
    } else if (event.key.toLowerCase() === "l" && !$("toggle-log").disabled) {
      event.preventDefault();
      toggleLogPanel();
    } else if (event.key === "[" && !$("undo-version").disabled) {
      event.preventDefault();
      $("undo-version").click();
    } else if (event.key === "]" && !$("redo-version").disabled) {
      event.preventDefault();
      $("redo-version").click();
    }
  });
  for (const button of document.querySelectorAll("[data-viewport]")) button.addEventListener("click", () => {
    for (const option of document.querySelectorAll("[data-viewport]")) option.setAttribute("aria-pressed", String(option === button));
    $("frame-host").classList.toggle("mobile", button.dataset.viewport === "mobile");
  });
  async function openSessionFromQuery() {
    const requestedSid = new URLSearchParams(window.location.search).get("sid");
    if (!requestedSid || !/^[0-9a-f]{32}$/.test(requestedSid)) return;
    try {
      const resp = await fetch(`/api/sessions/${requestedSid}`, { cache: "no-store" });
      if (!resp.ok) return;
      const detail = await resp.json();
      const currentVer = detail.versions.find((v) => v.n === detail.current) || detail.versions[detail.versions.length - 1];
      if (!currentVer) return;
      const controller = new AbortController();
      await showApp(
        { sid: detail.sid, version: currentVer.n, url: `/app/${detail.sid}/${currentVer.n}`, summary: currentVer.summary },
        controller.signal,
      );
      state.sid = detail.sid;
      state.version = currentVer.n;
      state.hasSessionImage = Boolean(detail.has_image);
      state.versions = detail.versions.map((v) => ({ n: v.n, summary: v.summary }));
      state.messages = detail.messages.map((m, idx) => ({
        role: m.role,
        text: m.text,
        version: m.role === "assistant" ? Math.floor(idx / 2) + 1 : undefined,
      }));
      renderChatMessages();
      $("status-dot").className = "ready";
      $("status-text").textContent = `Projeto v${state.version} aberto do Hub`;
      updateControls();
    } catch { /* ignore invalid sid query */ }
  }

  window.addEventListener("pagehide", () => { stopCamera(); clearImage(); });
  loadConfig();
  loadPrompts();
  openSessionFromQuery();
})();
