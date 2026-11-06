# Arquitetura

O J.A.R.V.I.S. é uma camada sobre o Odysseus, não uma reescrita. É um monólito modular: um processo FastAPI, o `src/jarvis/` e os serviços que a stack já tinha (ChromaDB, SearXNG e ntfy).

## Fontes da verdade

| Dado | Onde vive | Observação |
|---|---|---|
| Sessões, mensagens, tarefas, endpoints | `data/app.db` (SQLite) | Do Odysseus. O Jarvis só lê, exceto eventos do Google na agenda |
| Memórias, aprovações, missões, fila de e-mail, tokens, uso | `data/jarvis/jarvis.db` (SQLite) | Do Jarvis, em arquivo separado |
| Vetores | ChromaDB | Índice reconstruível a partir do SQLite (`/api/jarvis/memory/reindex`) |
| Configuração | `data/jarvis/config.json` | Sem segredos em texto aberto |
| Réplica opcional | Firestore | Nunca é a fonte da verdade |
| Objetos e backups | Disco local ou R2 | Backups são criptografados antes de sair |

Por que um banco próprio: uma migração do Jarvis não pode danificar o `app.db`, atualizações do Odysseus continuam possíveis e os dados do Jarvis sobrevivem à troca do motor.

## Módulos (`src/jarvis/`)

| Módulo | Responsabilidade |
|---|---|
| `policy.py`, `audit.py`, `redact.py` | Aprovação, auditoria e mascaramento. Toda ação externa passa por aqui |
| `router.py`, `llm.py` | Catálogo de modelos, regras de custo, ranking, fallback e uso |
| `memory.py`, `learning.py` | Memória de longo prazo e aprendizado |
| `intents.py`, `agenda.py` | Comandos respondidos sem modelo |
| `missions.py`, `worker.py`, `notify.py` | Tarefas persistentes e segundo plano |
| `google/`, `mailqueue.py` | OAuth, Calendar, Gmail, Drive, Fotos e fila de envio |
| `home.py`, `voice.py`, `browser.py` | Casa inteligente, voz e navegador |
| `storage/`, `firebase.py`, `replication.py`, `backup.py` | Armazenamento, réplica e backup |
| `observability.py`, `agent_tool.py` | Monitoramento e ferramenta do agente |

Pontos de contato com o Odysseus: `app.py` (registro de rotas e worker), `routes/chat_routes.py` (fallback, comandos diretos e uso), `src/chat_processor.py` (persona e memória), `src/agent_loop.py` (atalho sem ferramentas) e os registros de ferramentas.

Veja também [diagramas](diagrams.md) e [decisões](decisions.md).
