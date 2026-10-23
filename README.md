# Sketch2App ✏️ ➜ 🚀

> **Do papel para o próximo clique:** Transforme wireframes e rascunhos desenhados à mão em aplicações web completas, interativas e funcionais em segundos usando LLMs (Google Gemini).

Criado sob medida para apresentações e palestras ao vivo com efeito *"uau"*: o usuário mostra um rascunho na webcam (ou envia uma foto), opcionalmente adiciona instruções de estilo (ex: *"paleta azul e laranja"*), e um app funcional aparece instantaneamente no telão. Depois, um chat integrado permite ajustar o app em tempo real.

---

## 🌟 Principais Recursos

- **Efeito Telão ao Vivo**:
  - **Pipeline em tempo real**: visualização de cada etapa (`Foto` ➔ `Pré-processamento` ➔ `Gemini` ➔ `Validação` ➔ `App`) com cronômetro e tempos reais (`stage_timing`).
  - **Leitura do desenho**: lista de elementos identificados no papel pelo Gemini (`analysis`) exibidos um a um.
  - **Streaming de código e tokens**: contagem de caracteres e tokens estimados ➔ reais (`usage`) durante a geração.
- **Chat Iterativo com Versionamento**:
  - Ajustes de estilo e lógica por chat (`v1`, `v2`, `v3`...).
  - **Chips rápidos**: *"modo escuro"*, *"botões maiores"*, *"trocar paleta"*, *"adicionar animação"*.
  - **Reenvio de desenho**: toggle para corrigir a interpretação visual do rascunho.
  - **Desfazer / Refazer / Navegação entre versões sem chamada à IA** (troca instantânea e sem custo de tokens).
- **Ferramentas de Apresentação**:
  - **Modo Tela Cheia**: expande a aplicação para o projetor mantendo o estado da sessão intacto.
  - **Antes / Depois com Slider**: compare o rascunho original lado a lado com a aplicação gerada.
  - **Timelapse**: assista à evolução da ideia desde o primeiro esboço até a versão final.
  - **Ver e Copiar Código**: inspecione o HTML gerado diretamente pela interface.
  - **Baixar HTML Autônomo**: exporta um único arquivo `.html` com todas as dependências locais embutidas inline.
  - **Comando por Voz**: dite os ajustes do chat via Web Speech API.
  - **Modo Simples**: fallback de palco que desliga a telemetria e exibe apenas o app.
- **Catálogo Fechado de Bibliotecas Locais** (*Modo Caprichado*):
  - Tailwind CSS 3.4.17 local (sem CDN).
  - GSAP (animações), Chart.js (gráficos), Lucide (ícones), Alpine.js (interatividade reativa), Animate.css e fontes locais (Inter, Space Grotesk, JetBrains Mono).
- **Participação da Plateia & Hub (Fase 2)**:
  - **Página de Envio da Plateia (`/enviar`)**: espectadores escaneiam o QR Code exibido no telão e enviam fotos de seus próprios rascunhos pelo celular.
  - **Fila de Aprovação**: operador aprova ou descarta envios da plateia antes de gastar cota de IA.
  - **Hub de Projetos (`/hub`)**: galeria visual de todas as sessões criadas, com suporte a sincronização no Supabase.
  - **API de Dados por Sessão (`/api/data/{sid}/{colecao}`)**: backend SQLite local fornecido para os apps gerados salvarem itens via `fetch` sem depender de `localStorage`.
- **Plano B & Replay Offline**:
  - Gravação automática de sessões com métricas reais (`RECORD=true`).
  - Modo 100% offline para rodar a demo mesmo sem Wi-Fi ou caso a cota da API falhe (`/api/replay/{name}`).

---

## 🛠️ Stack Tecnológica

- **Backend**: Python 3.11+, FastAPI, Uvicorn, Pillow, `google-genai`, SQLite, Pydantic v2, Python-dotenv.
- **Frontend**: HTML5, CSS3 moderno, Vanilla JavaScript (sem etapa de build/bundler), Server-Sent Events (SSE).
- **Sandbox Seguro**: O código gerado roda estritamente isolado em `<iframe sandbox="allow-scripts">` (sem `allow-same-origin`).

---

## 🚀 Instalação e Execução

### 1. Pré-requisitos
- Python 3.11 ou superior instalado.
- Chave de API gratuita do [Google AI Studio](https://aistudio.google.com/).

### 2. Clonar e Instalar Dependências

```bash
# Criar e ativar o ambiente virtual (Windows PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# No Linux / macOS:
# python3 -m venv .venv
# source .venv/bin/activate

# Instalar dependências
pip install -r requirements.txt
```

### 3. Configurar Variáveis de Ambiente

Copie o arquivo de exemplo `.env.example` para `.env`:

```bash
cp .env.example .env
```

Preencha no seu `.env`:
```env
GEMINI_API_KEY=sua_chave_aqui
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_MODEL_FALLBACK=gemini-2.5-flash

# Para testar sem gastar cota, use:
# MOCK_LLM=true

# Para habilitar o QR Code do túnel (Fase 2):
# PUBLIC_BASE_URL=https://seu-tunel.trycloudflare.com
```

### 4. Executar o Servidor

```bash
uvicorn app.main:app --reload --port 8000
```

Acesse o estúdio no seu navegador: **[http://localhost:8000](http://localhost:8000)**.

---

## 🎯 Roteiro de Uso na Palestra

1. **Capturar o Desenho**:
   - Clique em **"Ativar câmera"** e aponte para a folha de papel (ou pressione a barra de **Espaço** para capturar).
   - Alternativamente, use a aba **"Enviar imagem"** para subir um arquivo JPG/PNG/WebP.
2. **Definir o Estilo** *(opcional)*:
   - Digite no campo de instrução: `paleta azul e laranja, cantos arredondados, visual limpo`.
3. **Gerar o App**:
   - Clique em **"Gerar meu app"** (ou use o atalho `Ctrl+Enter`).
   - Acompanhe a telemetria e o streaming do código até o app renderizar no painel direito.
4. **Iterar no Chat**:
   - Use os chips rápidos (ex: *"modo escuro"*) ou digite pedidos como *"troque os cards por uma tabela com busca"*.
   - Se a IA interpretar incorretamente alguma parte desenhada, marque a caixa **"Reenviar desenho"**.
5. **Navegar e Comparar**:
   - Use as pílulas `v1`, `v2`, `v3` ou os atalhos `[` e `]` para alternar entre as versões geradas.
   - Pressione `F` para entrar em tela cheia no projetor.

---

## ⌨️ Atalhos de Teclado (Modo Palco)

Quando o foco não estiver em um campo de digitação:

| Tecla | Ação |
|---|---|
| `Espaço` | Capturar foto da webcam |
| `Ctrl + Enter` | Gerar app ou enviar mensagem no chat |
| `F` | Alternar modo Tela Cheia |
| `C` | Abrir / fechar visualizador de código |
| `L` | Abrir / fechar log técnico de eventos |
| `[` | Desfazer / voltar para versão anterior |
| `]` | Refazer / avançar versão |
| `Esc` | Fechar código, comparador ou sair da tela cheia |

---

## 🧪 Suíte de Testes Automatizados

O projeto conta com mais de 280 testes unitários e de integração (sem chamadas à rede):

```bash
# Rodar todos os testes Python com checagem estrita de warnings
pytest -q -W error

# Rodar os testes do leitor SSE em Node.js
node --test tests/test_sse.cjs
```

---

## 📄 Licença

Este projeto é disponibilizado sob a licença [MIT](LICENSE).
