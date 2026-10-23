# PROGRESS — Sketch2App

Estado vivo do desenvolvimento. Fonte de verdade do projeto: `AGENTS.md`. Atualizar ao concluir cada passo.

## Passos (ordem do AGENTS.md)
- [x] 1. Setup (venv, requirements, .env.example, .gitignore, tailwind local)
- [x] 2. imaging.py + parse_output + test_parse.py
- [x] 3. llm.py (Gemini, rate limiter, fallback, logs de tokens)
- [x] 3b. Mock do LLM (MOCK_LLM=true), validado sem chave/modelo e sem API real
- [x] 4. sessions.py + POST /api/sessions + GET /app/{sid}/{n}
- [x] 5. Painel + webcam + prompt de estilo (aceite local com mock; ensaio físico/real pendente)
- [x] 6. SSE (status + progress)
- [x] 6b. Adaptação antes do chat (ANALYSIS opcional + eventos SSE analysis/code/stage_timing/usage + metadados por versão)
- [x] 7. Chat (/messages, histórico, toggle reenviar desenho, chips)
- [x] 8. Versões (/restore, desfazer/refazer)
- [x] 9. Cache / offline / replay
- [x] 10. Polimento de palco
- [x] 11. Extras de palco (painel do telão, antes/depois, timelapse, código em streaming, baixar HTML, voz...)
- [x] 12. Bibliotecas permitidas + modo caprichado
- [x] 13A. Fase 2 — Item A: Hub de projetos (`storage.py`, `/hub`, sincronização Supabase)
- [x] 13B. Fase 2 — Item B: Túnel gratuito + participação da plateia (`/enviar`, `/upload`, fila de aprovação)
- [x] 13C. Fase 2 — Item C: API de dados por sessão (`/api/data/{sid}/{colecao}`)

## Configuração atual
- Python 3.13.15; ambiente virtual em `.venv/`, dependências instaladas e versões fixadas em `requirements.txt`.
- `.env` recriado a partir de `.env.example` nesta cópia: chave e modelos vazios; preencher localmente para geração real.
- Perfil inicial: `MIN_INTERVAL_S=7`, thinking não configurado, imagem 1024 e saída máxima 16000 tokens.
- `MOCK_LLM=true` ativado no `.env` para desenvolver sem consumir cota; `OFFLINE_MODE=false`.
- Para usar a API real ou o smoke test, definir `MOCK_LLM=false` e reiniciar o processo que carregou o `.env`.
- Iniciar no PowerShell: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000`.

## Decisões novas (além do AGENTS.md)
- Contexto alinhado às interfaces existentes e ao comportamento já validado; detalhes de contratos e decisões ficam no `AGENTS.md`.

## Setup desta cópia — 2026-10-22
- Python 3.13.15 instalado/atualizado pelo winget; `.venv` criado e dependências de `requirements.txt` instaladas, sem conflitos (`pip check`).
- `.env` criado sem credenciais, com simulação habilitada. Tailwind local existente preservado; nenhum código funcional alterado.
- Validação: `python -m pytest -q -W error -x --tb=short`: **230 testes passaram em 2,84 s**. Uvicorn iniciado em `http://127.0.0.1:8000`; painel e `/vendor/tailwind.js` retornaram HTTP 200; `/api/config` confirmou modo mock.
- Zero chamadas ao Gemini. Esta pasta não contém repositório Git; nenhum repositório foi inicializado.

## Pendências / bugs conhecidos
- Próximo passo de implementação: etapa 10, polimento de palco (tela cheia, contraste/fontes de projetor, atalhos e "Ver código").
- Antes do ensaio, validar a etapa 5 com webcam física e `MOCK_LLM=false`: interpretação do desenho e aplicação de "paleta azul e vermelho". O mock valida a integração, mas entrega HTML fixo. Nenhuma chamada real foi feita nesta etapa.
- Confirmar cotas efetivas de RPM/TPM/RPD no AI Studio antes do ensaio. O intervalo local atual é de 7 s; considerar seu impacto na meta de latência do chat.
- Etapas posteriores, bibliotecas extras e fase 2 ainda não implementadas. Nenhum bug conhecido nas funcionalidades já validadas.

## Evidências recuperadas das etapas 1–3
- Setup validado: Uvicorn iniciou, página e Tailwind local retornaram HTTP 200, dependências sem conflitos.
- Processamento de imagens e parser: 67 testes sem rede na etapa 2.
- Etapa 3 validada com imagem sintética de lista de tarefas: uma consulta de metadados confirmou os modelos visíveis para a chave; a tentativa no 3.8 Flash retornou 503 e duas chamadas ao Flash-Lite produziram HTML válido.
- A primeira prévia revelou dependência de submit nativo bloqueado pelo sandbox. O prompt foi corrigido; a nova prévia foi aprovada no Edge: adicionar por clique/Enter, concluir, remover, filtrar e atualizar contadores, com Tailwind local e sem exceções JS detectadas.
- Prévia real aprovada: `generated/smoke/399318a9e701477ea0f091f334ecf36c/index.html`; screenshot no mesmo diretório. Imagem sintética: `generated/fixtures/lista-tarefas.png`.
- Suíte anterior à etapa 3b: 136 testes passando. As medições reais abaixo são das chamadas à API, não do futuro fluxo completo de captura/UI.

## Validação da etapa 3b — 2026-10-22
- Adicionado mock de execução à interface existente, template local interativo e proteção do smoke test contra simulação. Sem novas dependências ou rotas.
- `AGENTS.md` sincronizado com o código: nome/retorno da função, configuração de thinking, parser, correção de formulários, critérios de aceite e transição JSON/SSE. As novas etapas 11–13 foram preservadas.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **150 testes passaram em 4,45 s**, sem avisos.
- Testes novos cobrem ativação por ambiente, geração sem credenciais, parser compartilhado, variação segura do resumo, isolamento das métricas reais, alternância entre modos, busy, cancelamento, prioridade offline e falhas controladas do mock.
- Aceite direto: chave/modelo apagados apenas no ambiente do processo de verificação e criação do cliente Gemini proibida; geração válida em 0,211 s, zero chamadas reais e nenhum timestamp/counter da API alterado. O `.env` manteve as credenciais existentes.
- Prévia de mock em `generated/mock-preview/index.html` aprovada no Edge em iframe sandbox: Tailwind local carregou, contador incrementou e zerou, sem exceções JS. Screenshot no mesmo diretório; processos de verificação encerrados.
- Nenhuma chamada real ao Gemini nesta etapa. Sessões e replay offline permanecem para suas respectivas etapas.

## Validação da etapa 4 — 2026-10-22
- Sessões e v1 persistidas em disco, com metadados tipados, imagens original/preparada e histórico inicial; publicação atômica e recuperação sem depender da memória do processo.
- Implementados `POST /api/sessions` (201/JSON) e `GET /app/{sid}/{n}` (HTML com política de sandbox). Contratos, limites e formato em disco registrados no `AGENTS.md`.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **221 testes passaram em 6,55 s**, sem avisos. Testes HTTP usam ASGI diretamente, sem adicionar dependências de teste.
- Cobertura nova: entrada texto/imagem, base64 e limite de tamanho, preparação do JPEG, persistência e reinício, publicação/rollback, dados inválidos/corrompidos, controle de caminhos e versões, erros seguros, busy e cancelamento antes da gravação. Rotas estáticas e proteção de `generated/` verificadas.
- Aceite com Uvicorn real, credenciais vazias no processo e criação do cliente Gemini proibida: POST com desenho sintético retornou 201 em **0,302 s**; GET retornou 200. Após reiniciar o servidor, o HTML recuperado foi idêntico.
- Sessão de aceite: `d8334621764b42ed8a0416baf2e858c5`; URL `/app/d8334621764b42ed8a0416baf2e858c5/1`. Imagem original, JPEG, metadados e HTML presentes em `generated/<sid>/`.
- Edge abriu essa URL em iframe sandbox, carregou Tailwind local e validou incremento/reset do contador, sem exceções JS. Screenshot em `generated/session-preview/preview.png`; processos temporários encerrados.
- Zero chamadas reais ao Gemini. `MOCK_LLM=true` e demais parâmetros do `.env` preservados; não foi revalidada geração real nem iniciada a etapa 5.

## Validação da etapa 5 — 2026-10-22
- Painel entregue com identidade de estúdio (papel, verde e lima), ilustração em CSS, layout responsivo, webcam/seletor, captura por botão/Espaço, upload/arrastar, instruções e prévia interativa isolada. Contratos e decisões registrados no `AGENTS.md`; nenhuma dependência nova.
- Adicionado `GET /api/config` com resposta pública mínima, testada para modos mock/live/offline, precedência offline e ausência de credenciais. O servidor existente em `http://127.0.0.1:8000` já respondeu 200 com modo mock após a alteração.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **230 testes passaram em 10,15 s**, sem avisos. `node --check web/app.js` também passou.
- Aceite em Edge headless com Uvicorn real, credenciais vazias no processo de verificação e criação do cliente Gemini proibida: câmera por stream de canvas, captura JPEG, seletor, respeito à digitação no atalho Espaço, encerramento de tracks, upload PNG e geração somente por texto.
- Confirmados: prompt de paleta e base64 puro no POST, bloqueio de envio duplicado durante espera, carregamento do Tailwind local, contador/reset do app no iframe, origem opaca e sandbox somente com allow-scripts. Alterar largura da prévia não recria o iframe nem faz POST.
- Testados erro 503 e imagem corrompida (preservam prévia/seleção anterior), restauração dos controles e bloqueio de geração no modo offline. Nenhuma exceção JavaScript detectada.
- Sem overflow horizontal nas larguras 320, 360, 390, 560, 768, 850, 1024, 1440 e 1920 px; resultado gerado também verificado em cinco larguras. Capturas desktop/celular revisadas em `generated/frontend-check/`; relatório em `report.json` e script local em `generated/verify_frontend.py` (artefatos ignorados).
- Corrigida a especificidade dos estilos do mock para que o preflight do Tailwind não remova a aparência do título e dos botões. Verificação no navegador confirmou o estilo e a interação após o carregamento.
- **Zero chamadas reais e zero tokens de API**. `.env` preservado, com `MOCK_LLM=true`. As esperas controladas do teste de envio duplicado não representam latência do Gemini. A webcam física e a paleta em geração real seguem pendentes; SSE/chat/versões/replay não foram iniciados.

## Validação da etapa 6 — 2026-10-22
- Geração migrou de 201/JSON para SSE; painel mostra etapa, caracteres recebidos e log expansível. Contrato atual e assinatura compatível de `generate_app` registrados no `AGENTS.md`.
- Streaming real usa o SDK existente, com agregação de texto/metadados, preservação do timeout total, intervalo e orçamento de retries. Mock transmite quatro blocos identificados como simulação, sem credenciais/API. Nenhuma dependência instalada nesta etapa.
- `python -m pytest -q -W error -x --tb=short`: **242 testes passaram em 2,98 s**, sem avisos. `node --test tests/test_sse.cjs`: **6 testes passaram**. Sintaxe dos dois arquivos JS e `pip check` aprovados.
- Cobertura nova: entrega incremental antes de persistir, sequência de eventos, busy, erros seguros, desconexão ASGI 2.3/2.4 e cancelamento durante gravação; texto/thinking, metadados finais, truncamento, bloqueio, fallback, correção e timeout no streaming com doubles sem rede.
- Aceite em Edge headless + Uvicorn real com mock: contador avança antes de abrir iframe; envio bloqueado durante espera; erro com hint offline e stream interrompido preservam o mesmo iframe; nova tentativa recupera o fluxo sem reconexão automática. Sandbox `allow-scripts` mantido; nenhuma exceção JS. Sem overflow a 320, 390, 850, 1440 e 1920 px.
- Aceite HTTP direto observou primeiros status em 48 ms, quatro eventos progress entre 97 e 288 ms e done em 292 ms. Medição local de mock, sem valor de latência do Gemini. **Zero chamadas reais e zero tokens de API**; ensaio de streaming com Gemini e webcam física permanece pendente.
- Artefatos locais ignorados em `generated/sse-check/`: relatório, screenshot revisado e script do navegador. Servidor atualizado em `http://127.0.0.1:8000` com reload; chat/restore/replay não iniciados.

## Preparação para salvar/compartilhar — 2026-10-22
- `.gitignore` ampliado para proteger gravações de cache, relatórios de cobertura, caches de ferramentas, configurações pessoais e ZIP de distribuição. Credenciais, ambiente virtual e sessões locais foram preservados no computador.
- Removido apenas `tests/.gitkeep`, redundante porque a pasta já contém testes.
- Cópia limpa em `sketch-para-subir.zip`: fontes, testes, contexto, dependências fixadas, `.env.example` e Tailwind local. Exclui dados de execução e arquivos pessoais; conteúdo conferido e comparado com as fontes. Não foi inicializado repositório nem realizado envio remoto.

## Validação da etapa 6b — 2026-10-22
- Contrato estendido com `ParsedOutput.analysis` (padrão `[]`), tolerância restrita ao bloco opcional `ANALYSIS` (máx. 12 itens e 80 caracteres por item, sem retry quando ausente/malformado) e solicitação de `ANALYSIS` apenas quando há imagem na geração inicial (`SYSTEM_GENERATE` vs `SYSTEM_GENERATE_TEXT`).
- Callback de streaming estendido na mesma interface `generate_app` com `analysis`, `progress.est_tokens`, `code` (somente HTML, agrupado em ~150 ms / 96 caracteres e descarregado ao fim), `stage_timing` (`preparando`, `enviando` com `wait_ms` separado da espera obrigatória, `gerando`, `validando`, `gravando`) e `usage` por tentativa (usando o `usage_metadata` final sem somar contadores cumulativos).
- Persistência por versão (`VersionInfo`) enriquecida com metadados opcionais `analysis`, `usage` e `stage_timing`; sessões legadas continuam abrindo sem migração destrutiva.
- Leitor SSE (`web/sse.js`) ajustado para aplicar o limite de 64 KiB por evento incompleto (aceitando múltiplos eventos agrupados numa leitura) e isolar falhas de apresentação dos eventos opcionais.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **262 testes passaram em 3,44 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real com mock (`generated/sse-check/verify.mjs`): geração somente texto (sem `analysis`, com `est_tokens`, `code`, `stage_timing` e `usage`) e geração com imagem (com `analysis` e registro de "Leitura do desenho"), erro com hint offline, stream interrompido, preservação da prévia anterior, recuperação na tentativa seguinte e zero overflow a 320, 390, 850, 1440 e 1920 px. **Zero chamadas reais ao Gemini**.
- Contexto consolidado em `AGENTS.md` e `PROGRESS.md`; arquivos temporários `AGENTS2.md` e `PROGRESS2.md` removidos após incorporação integral. Próximo passo: etapa 7 (Chat).

## Validação da etapa 7 — 2026-10-22
- Implementado o fluxo de chat (`POST /api/sessions/{sid}/messages` via SSE e `GET /api/sessions/{sid}` via JSON) com `SYSTEM_CHAT`, envio do HTML da versão atual, histórico truncado às últimas `HISTORY_TURNS` trocas (apenas mensagem do usuário e `SUMMARY` da resposta, sem HTML antigo) e reenvio opcional do JPEG preparado (`reattach_image=true`).
- Persistência estendida em `SessionStore` com `save_version` (publicação atômica de `vN.html` e `session.json`, com rollback em caso de falha) e `read_prepared_image`.
- Painel (`web/index.html`, `web/style.css`, `web/app.js`) atualizado com histórico de conversa (`textContent`), campo de ajuste com suporte a Enter/botão, toggle "Reenviar desenho" (desligado por padrão e desabilitado em sessões sem imagem) e os quatro chips de ajuste rápido ("modo escuro", "botões maiores", "trocar paleta para verde e laranja", "adicionar animação sutil").
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **270 testes passaram em 3,40 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real com mock (`generated/sse-check/verify.mjs`): geração inicial v1 → clique no chip "modo escuro" gerando v2 (`reattach_image=false`) → envio pelo chat de "esse retângulo era uma tabela" com toggle "Reenviar desenho" ligado gerando v3 (`reattach_image=true`), atualização do badge (`v2`, `v3`), iframe em sandbox e zero overflow de 320 a 1920 px. **Zero chamadas reais ao Gemini**.

## Validação da etapa 8 — 2026-10-22
- Implementado `SessionStore.restore_version` em `app/sessions.py` (valida a existência do HTML da versão em disco e persiste `current` de forma atômica em `session.json`) e a rota `POST /api/sessions/{sid}/restore` (JSON) em `app/main.py` com schema estrito `RestoreVersionRequest`, proteção por `_generation_lock` e log explícito `api_calls=0`.
- Ao restaurar uma versão anterior (ex.: voltar da v2 para a v1) e enviar novo ajuste pelo chat, a geração usa o HTML da versão restaurada (`v1.html`) e publica `v3` sem sobrescrever `v2`.
- Painel (`web/index.html`, `web/style.css`, `web/app.js`) atualizado com a barra de versões (`v1 · v2 · v3` clicáveis com `aria-pressed`) e botões **Desfazer** e **Refazer** integrados a `/api/sessions/{sid}/restore`, sem chamar a LLM.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **273 testes passaram em 3,16 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real com mock (`generated/sse-check/verify.mjs`): após criar `v1`, `v2` e `v3`, validados clique em **Desfazer** (`v3 → v2`), clique direto em `v1` (`v2 → v1`) e clique em **Refazer** (`v1 → v2`), confirmando `restoreCalls === 3` e `testCalls` inalterado (`7`, zero chamadas extras de geração), atualização do iframe/badge e zero overflow de 320 a 1920 px. **Zero chamadas reais ao Gemini**.

## Validação da etapa 9 — 2026-10-23
- Implementada a gravação automática em `cache/<sid>.json` quando `RECORD=true` (`LLMSettings.record`, `SessionStore.export_to_cache`, `load_cached`, `list_cache`) e as rotas `GET /api/cache` e `POST /api/replay/{name}`.
- O replay reconstrói o streaming SSE (`status`, `stage_timing`, `analysis` na v1 quando presente, `progress.est_tokens`, `code` em blocos a partir do HTML salvo e `done`) via `stream_recorded_version`, sem tocar na API do Gemini. Quando a gravação não possui métricas (`usage`), nenhuma métrica inventada é emitida e o painel exibe informação indisponível.
- Sessões abertas via replay registram `replay_source` nos metadados e permitem executar também os turnos de chat gravados (`v2`, `v3`...) e `/restore` mesmo com `OFFLINE_MODE=true`.
- Painel (`web/index.html`, `web/style.css`, `web/app.js`) atualizado com o botão **Modo offline** no cabeçalho e painel de reprodução com seletor de gravações e botão **Reproduzir gravação**.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **276 testes passaram em 3,84 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real (`generated/sse-check/verify.mjs`): sessão gravada com `RECORD=true` listada em `/api/cache`, reproduzida via `POST /api/replay/{name}` (`v1`) e avançada para `v2` pelo chat sem chamadas extras à LLM, com zero overflow de 320 a 1920 px. **Zero chamadas reais ao Gemini**.

## Validação da etapa 10 — 2026-10-23
- Painel (`web/index.html`, `web/style.css`, `web/app.js`) atualizado com controles de palco na barra da prévia: **Ver código** (`#toggle-code`), **Log técnico** (`#toggle-log`) e **Tela cheia** (`#toggle-fullscreen`).
- Visualizador de código (`#code-panel`) exibe o HTML da versão ativa como texto puro (`textContent`), contagem de linhas e tamanho em KB, botão **Copiar HTML** e fechamento por botão ou `Escape`.
- Modo tela cheia expande `.preview-card` na viewport inteira sem mover ou recriar o `<iframe sandbox="allow-scripts">`, preservando o contexto de navegação e o estado do app gerado.
- Atalhos de teclado de palco implementados quando o foco está fora de campos de digitação: `Espaço` (capturar), `Ctrl+Enter` (gerar/enviar chat), `F` (tela cheia), `C` (ver código), `L` (log técnico), `[` / `]` (desfazer/refazer versão) e `Escape` (sair da tela cheia / fechar código).
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **276 testes passaram em 3,36 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real (`generated/sse-check/verify.mjs`): abertura/fechamento de código (`Escape`), alternância de tela cheia (`F` / `Escape`) preservando a mesma instância de iframe, alternância de log (`L`), navegação de versões por atalhos (`[` / `]`) e zero overflow de 320 a 1920 px. **Zero chamadas reais ao Gemini**.

## Validação da etapa 11 — 2026-10-23
- **Painel do telão** (`#stage-screen`): pipeline animado em 5 etapas com tempos reais (`stage_timing`), desenho em destaque com linha de varredura animada (respeitando `prefers-reduced-motion` e com alternativa para sessões somente por texto), lista **"Leitura do desenho"** (itens do evento `analysis`, ou `"Análise indisponível"` quando ausente), código em streaming (`textContent`), contador de tokens estimado → real (`usage`) e timer (`00:00`).
- **Cartão final** (`#stage-summary-card`) e toggle **Modo simples** (`#toggle-simple-mode`): controla apenas a apresentação (ocultando pipeline, leitura do desenho, código em streaming e cartão final para exibir somente status + app), sem nova geração.
- **Antes/depois** (`#compare-panel` + `GET /api/sessions/{sid}/image`): comparador com slider sobreposto sem mover ou recriar o iframe da prévia.
- **Timelapse** (`#timelapse-button`): percorre `v1 → v2 → v3...` via `/restore` com zero chamadas à LLM.
- **Baixar HTML** (`#download-html` + `GET /api/sessions/{sid}/download/{n}`): exporta arquivo HTML autônomo com os ativos de `/vendor/` (`tailwind.js`) embutidos inline.
- **Voz** (`#voice-button` via Web Speech API com degradação graciosa), **Estilos em um clique** (`retrô 8-bit`, `neon`, `minimalista`, `vidro`), **Painel de bastidores** (`GET /api/prompts`, sem chaves, com tokens e latência) e `allow="gamepad"` no iframe em sandbox.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **277 testes passaram em 4,13 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real (`generated/sse-check/verify.mjs`): todos os extras validados, sem overflow de 320 a 1920 px e **zero chamadas reais ao Gemini**.

## Validação da etapa 12 — 2026-10-23
- Catálogo fechado de bibliotecas locais baixado para `web/vendor/` com versões fixadas e documentadas em `web/vendor/VERSIONS.md`: `tailwind.js` (3.4.17), `gsap-3.12.5.min.js`, `chart-4.4.7.umd.min.js`, `lucide-0.469.0.min.js`, `alpine-3.14.8.min.js`, `animate-4.1.1.min.css` e `fonts.css` (`inter-400.woff2`, `space-grotesk-700.woff2`, `jetbrains-mono-400.woff2`).
- Mount `/vendor` configurado via `VendorStaticFiles` respondendo `Access-Control-Allow-Origin: *` (necessário para fontes `.woff2` dentro do iframe em sandbox com origem opaca); download de HTML autônomo (`GET /api/sessions/{sid}/download/{n}`) embute scripts, estilos e fontes `.woff2` inline.
- `parse_output` em `app/llm.py` rejeita qualquer `<script src>` ou `<link href>` que não comece com `/vendor/` ou não pertença ao catálogo permitido (`ALLOWED_VENDOR_FILES`), acionando a tentativa única de reparo com aviso para usar só o catálogo.
- Toggle **Modo caprichado** (`#toggle-extras-mode`, desligado por padrão) adicionado ao cabeçalho: quando ligado (`use_extras=true`), anexa `SYSTEM_EXTRAS` ao prompt de sistema na geração e no chat.
- `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **284 testes passaram em 4,41 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**.
- Aceite em Edge headless + Uvicorn real (`generated/sse-check/verify.mjs`): ativação do Modo caprichado, geração com bibliotecas de `/vendor/` (GSAP + Chart.js + fontes locais), atualização do painel de bastidores e zero overflow de 320 a 1920 px. **Zero chamadas reais ao Gemini**.

## Ensaio real com Gemini (`gemini-3.5-flash-lite`) e Validação da etapa 13A (Hub Supabase) — 2026-10-23
- **Seleção de modelos para o Free Tier**: `GEMINI_MODEL=gemini-3.5-flash-lite` (principal: ~30 RPM, baixa latência) e `GEMINI_MODEL_FALLBACK=gemini-2.5-flash` (fallback GA em cota separada), mantendo `THINKING_BUDGET` vazio.
- **Ensaio real (3 chamadas reais à API)**:
  1. `POST /api/sessions` (desenho `lista-tarefas.png` + `"paleta azul e vermelho"`): **4,806 s** de latência da LLM (`1558 tokens_in / 2013 tokens_out`, 6 itens em `analysis`, dentro da meta ≤ 15 s).
  2. `POST /api/sessions/{sid}/messages` (`"modo escuro"`, `reattach_image=false` → `v2`): **6,006 s** de latência da LLM (`2198 tokens_in / 2828 tokens_out`, dentro da meta ≤ 10 s).
  3. `POST /api/sessions/{sid}/messages` (`"esse bloco de tarefas deve mostrar prioridade Alta, Média ou Baixa"`, `reattach_image=true` → `v3`): **7,208 s** de latência da LLM (`4214 tokens_in / 3324 tokens_out`, dentro da meta ≤ 10 s).
  4. `POST /api/sessions/{sid}/restore` (`v1 → v3`): `api_calls=0`. Sessão completa gravada em `cache/ensaio-lista-tarefas.json` (67 KB).
- **Fase 2 — Item A (`app/storage.py`, `supabase_schema.sql`, `GET /api/hub`, `GET /hub`)**:
  - Interface unificada (`save_session`, `list_sessions`, `load_session`) com implementações `DiskStorage` (padrão local-first) e `SupabaseStorage` (sincronização automática com bucket privado `sketches` e tabelas `projects`/`versions`/`messages` via `SUPABASE_SERVICE_KEY` apenas no backend).
  - Bucket privado `sketches` criado e validado contra o projeto Supabase real configurado no `.env`; sessão real `adcd392d366a4d798844db0dc536bf3b` (`v1`, `v2`, `v3` + imagem) sincronizada e listada com `synced_cloud=True`.
  - Página `/hub` (`web/hub.html`) com galeria de projetos (miniatura do desenho, título, data, versões e abertura no estúdio via `/?sid=<sid>`).
  - `.\.venv\Scripts\python.exe -m pytest -q -W error -x --tb=short`: **285 testes passaram em 4,79 s**, sem avisos.

## Validação das etapas 13B e 13C (Fase 2 — Túnel/Plateia e API de dados por sessão) — 2026-10-23
- **Fase 2 — Item B (`app/audience.py`, `app/qrcode_svg.py`, `web/enviar.html`, `_TunnelGuardMiddleware`)**:
  - Middleware ASGI de isolamento do túnel: quando a requisição vem por túnel externo (`Cf-Connecting-Ip`, `X-Forwarded-For`, `X-Forwarded-Host` ou `Host` não local), expõe **somente** `GET /enviar`, `POST /upload`, `GET /app/{sid}/{n}`, `GET /vendor/*` e `/api/data/{sid}/{colecao}`, bloqueando `/`, `/hub`, `/api/sessions`, `/api/queue` e demais rotas do operador com HTTP 403 (`code=forbidden`).
  - Fila de aprovação em `generated/queue/` (`AudienceQueue`): rate limit por IP, limite de tamanho (10 MiB), validação de formato (`JPEG`, `PNG`, `WEBP`) e reprocessamento obrigatório via Pillow (`imaging.preprocess_image`) antes de persistir, sem chamar a LLM.
  - QR Code SVG ISO/IEC 18004 gerado em Python puro (`app/qrcode_svg.py`) a partir de `PUBLIC_BASE_URL` e exibido no painel **Plateia** (`#audience-panel`) com botões **Aprovar** (carrega o desenho pré-processado e a nota de estilo no estúdio) e **Descartar**.
- **Fase 2 — Item C (`app/session_data.py`, `GET/POST/PUT/DELETE/OPTIONS /api/data/{sid}/{colecao}`)**:
  - Persistência SQLite por sessão (`generated/session_data.sqlite3`) com conexões fechadas via `contextlib.closing` a cada operação, CORS liberado (`Access-Control-Allow-Origin: *`) para iframes em sandbox sem `allow-same-origin` e injeção automática de `window.SKETCH_SID` ao servir `GET /app/{sid}/{n}`.
  - Toggle **API de dados** (`#toggle-data-api`): quando ligado (`use_data_api=true`), anexa `SYSTEM_DATA_API` ao prompt de sistema na geração e no chat.
- `.\.venv\Scripts\python.exe -m pytest -q -W error`: **288 testes passaram em 4,80 s**, sem avisos. `node --test tests/test_sse.cjs`: **9 testes passaram**. Verificação E2E em Edge headless (`generated/sse-check/verify.mjs`) aprovada sem exceções JS e sem overflow de 320 a 1920 px.

## Medições do ensaio
| Cenário | Latência | Tokens in/out | Observação |
|---|---|---|---|
| Lista de tarefas sintética; 3.8 Flash | 1,967 s | não informados | Histórico: HTTP 503, sem retry automático |
| Lista de tarefas sintética; 3.5 Flash-Lite | 5,911 s | 1445 / 2043 | Histórico: HTML válido; submit no sandbox identificado |
| Lista de tarefas sintética; Flash-Lite, prompt corrigido | 5,593 s | 1535 / 2019 | Histórico: prévia funcional aprovada no Edge |
| Mock de desenvolvimento sem chave/modelo | 0,211 s | 0 / 0 | Latência simulada; nenhuma chamada real |
| HTTP: desenho → sessão/v1, com mock | 0,302 s | 0 / 0 | Inclui preparação e gravação; não representa latência do Gemini |
| Ensaio SSE v1 (`gemini-3.5-flash-lite`, imagem + paleta) | 4,806 s | 1558 / 2013 | 6 itens em `analysis`; meta ≤ 15 s cumprida |
| Ensaio SSE v2 chat (`"modo escuro"`, sem imagem) | 6,006 s | 2198 / 2828 | Meta ≤ 10 s cumprida |
| Ensaio SSE v3 chat (`reattach_image=true`, prioridades) | 7,208 s | 4214 / 3324 | Meta ≤ 10 s cumprida |

## Desenhos de ensaio e cache gravado
- Desenho sintético local em `generated/fixtures/lista-tarefas.png`; não é foto de webcam.
- Gravação real completa (v1, v2, v3, imagem, análise e métricas) salva em `cache/ensaio-lista-tarefas.json`.
