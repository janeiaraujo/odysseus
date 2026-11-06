# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Odysseus is a self-hosted AI workspace: a FastAPI backend with SQLite, ChromaDB and SearXNG, and a vanilla-JS frontend with no build step. It is licensed AGPL-3.0-or-later.

## Commands

```bash
# Setup (Python 3.11+; CI uses 3.11, the Docker image uses 3.14). pytest is in requirements.txt.
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# Full stack (app on :7000; the first admin password is printed in the logs)
cp .env.example .env && docker compose up -d --build
docker compose logs --tail=120 odysseus

# Tests: use the venv interpreter, because system Python gives misleading import errors
./venv/bin/python -m pytest tests/test_tool_policy.py                 # one file
./venv/bin/python -m pytest tests/test_tool_policy.py::test_name      # one test
./venv/bin/python tests/run_focus.py --area security --fast           # focused slice
./venv/bin/python tests/run_focus.py --area services --sub-area cookbook
./venv/bin/python tests/run_focus.py --last-failed
./venv/bin/python tests/run_order_report.py --seed 123 -- tests/cli/ -q   # shuffled order, finds isolation bugs
node --test tests/streaming/*.test.mjs                                # streaming-render suite; needs the glob

# The checks CI enforces (there is no linter or formatter config)
python -m compileall -q app.py core routes src services scripts tests
shopt -s globstar nullglob; for f in static/app.js static/js/**/*.js; do node --check "$f"; done

# CLIs: `git`-style dispatcher over scripts/odysseus-<name>
./venv/bin/python scripts/odysseus            # list subcommands
```

Test areas are `security`, `routes`, `services`, `cli`, `js`, `helpers`, `unit` and `uncategorized`. `tests/conftest.py` assigns them from the filename at collection time, along with a finer `sub_*` marker. `--fast` is for quick feedback only and does not replace a broader run.

The CI pytest job is `continue-on-error` because of known flaky and environment-dependent failures, so a green check does not mean the tests passed.

### Running the app locally without touching real data

`app.py` calls `load_dotenv()`, and `.env` usually sets `DATABASE_URL=sqlite:///./data/app.db`. **Setting `ODYSSEUS_DATA_DIR` alone does not isolate the database.** A local boot would open the same `data/app.db` that a running Docker stack has bind-mounted, and it would start a second task scheduler and email pollers against it. Override the database and turn off the background workers explicitly (variables already exported take precedence over `.env`):

```bash
ODYSSEUS_DATA_DIR=/tmp/ody-dev DATABASE_URL=sqlite:////tmp/ody-dev/app.db \
ODYSSEUS_INPROCESS_TASKS=0 ODYSSEUS_INPROCESS_POLLERS=0 \
./venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7001
```

This still connects to any ChromaDB on `localhost:8100`, so avoid memory and RAG writes while a real stack is running. Importing `core.database` runs `init_db()` and its migrations, so even a short-lived process writes to whichever DB it is pointed at. Tests are protected because `tests/conftest.py` defaults `DATABASE_URL` to in-memory SQLite.

## Architecture

**Wiring.** `app.py` is the composition root. It runs top to bottom at import: middleware, then `src/app_initializer.py:initialize_managers`, then `app.include_router(setup_<x>_routes(deps...))` for each feature. Route modules are factories that receive their managers as arguments rather than importing singletons. Startup and shutdown work is in `_startup_event` and `_shutdown_event`.

**Layers.** `routes/` holds HTTP handlers and `*_helpers.py`, `src/` holds domain logic, and `core/` holds the DB, auth and middleware. The layering isn't strict: `src/tool_implementations.py` imports from `routes/` inside function bodies, so watch for circular imports. `specs/architecture-runtime-inventory.md` maps these boundaries, but its counts are a snapshot, and its `conf/` entry no longer exists.

**Canonical modules and shims.** Check which copy is live before editing:
- `src/constants.py` is canonical; `core/constants.py` re-exports it.
- `core/database.py` is canonical (all ORM models); `src/database.py` re-exports it.
- `services/search/*` and `services/youtube/*` are canonical; the matching `src/` modules are wrappers, some of which replace themselves through `sys.modules`.
- `memory` and `research_handler` exist under both `src/` and `services/`. Confirm which one the call site imports.

**Agent request path.** `POST /api/chat_stream` (`routes/chat_routes.py`) resolves the endpoint, tool policy and workspace, then iterates `src/agent_loop.py:stream_agent_loop`. That generator yields the SSE events the frontend renders (`delta`, `tool_start`, `tool_output`, `agent_step`, `metrics`, `[DONE]`). Each round works like this:
1. `src/llm_core.py` makes the provider call, including per-provider quirks.
2. Tool calls are extracted, either native function calls (`src/tool_schemas.py:function_call_to_tool_block`) or calls parsed from model text (`src/tool_parsing.py`). Both become a `ToolBlock`.
3. `src/tool_execution.py:execute_tool_block` dispatches each block to `do_*` functions in `src/tool_implementations.py`, low-level tools in `src/agent_tools/`, or MCP servers (`src/mcp_manager.py`, `mcp_servers/`).

Agent tools can reach admin-gated HTTP routes through an in-process loopback authenticated by `INTERNAL_TOOL_TOKEN`.

**Tools are selected per message.** `src/tool_index.py` embeds `BUILTIN_TOOL_DESCRIPTIONS` in ChromaDB and retrieves the top-K, plus `ALWAYS_AVAILABLE`. A tool with a schema but no description is never offered to the model. Adding a tool touches several registries:
- `FUNCTION_TOOL_SCHEMAS` (`src/tool_schemas.py`)
- `BUILTIN_TOOL_DESCRIPTIONS` (`src/tool_index.py`); `tests/test_tool_index_schema_parity.py` checks this against the schemas
- `TOOL_TAGS` (`src/agent_tools/__init__.py`); calls to names missing here are rejected as unknown
- the dispatch branch in `_execute_tool_block_impl` (`src/tool_execution.py`)
- `NON_ADMIN_BLOCKED_TOOLS` and the plan-mode lists (`src/tool_security.py`)

Grep for an existing tool such as `manage_notes` to see every place one appears.

**Persistence.** SQLite through SQLAlchemy, with no Alembic. Schema changes are idempotent `_migrate_*` functions in `core/database.py` that run `ALTER TABLE` when a column is missing. Some state lives outside the DB: files under `DATA_DIR` (auth and sessions JSON written through `core/atomic_io.py`; settings through `src/settings.py`), and vectors in ChromaDB.

**Background work runs in the web process.** This includes `src/task_scheduler.py` (scheduled agent tasks, plus LLM-free actions in `src/builtin_actions.py`), the email pollers in `routes/email_pollers.py`, and `src/bg_monitor.py`. `ODYSSEUS_INPROCESS_TASKS=0` and `ODYSSEUS_INPROCESS_POLLERS=0` turn off the scheduler and pollers.

**Frontend.** Native ES modules, with no bundler or framework. `static/app.js` wires the modules in `static/js/` together, and `static/lib/` is vendored. `static/index.html` is served with `{{CSP_NONCE}}` substituted, so inline scripts need that nonce. If you change the precache list or service-worker logic, bump `CACHE_NAME` in `static/sw.js` and keep `PRECACHE` in sync with the `<script type="module">` tags in `index.html`.

**Other entry points.** `scripts/odysseus-*` are CLIs that import `core` and `src` directly, sharing `scripts/_lib/cli.py`. `companion/` is the LAN pairing API. `integrations/` holds the Claude Code and Codex skill bundles. `services/hwfit/` provides the hardware-aware model recommendations for Cookbook.

## Security invariants

`THREAT_MODEL.md` defines the trust boundary: admins may run shell commands by design, and the goal is to contain everyone and everything else.

- **Owner scoping:** multi-user data must be filtered with `src/auth_helpers.py:owner_filter`. Tests should show that one owner cannot see another owner's rows.
- **Route gates:** use `core/middleware.py:require_admin` and `src/auth_helpers.py:require_user` / `require_privilege`.
- **Non-admin tools:** anything privileged goes in `NON_ADMIN_BLOCKED_TOOLS`. Tools named `mcp__*` are blocked for non-admins automatically.
- **Prompt injection:** web results, fetched pages, emails, memories, skills, notes and external tool output must go through `src/prompt_security.py:untrusted_context_message`. Putting them in the system role is a security bug.
- **Reserved usernames:** `internal-tool` grants admin in `require_admin`, so it must stay in `RESERVED_USERNAMES` (`core/auth.py`).

## Code conventions

- **Paths:** every persisted file has a named constant in `src/constants.py`; add one if it's missing. Don't use `Path(__file__)` into the source tree, `/app/...`, relative `"data/..."`, or `DATA_DIR / "x.json"`. Use `DATA_DIR` directly only for dynamic per-owner paths. The source tree is read-only in Docker, so directory creation must fail gracefully rather than crash at import.
- **Loopback URLs:** use `internal_api_base()` from `src.constants`, never `http://localhost:7000`.
- **UI:**
  - Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, ...) and classes. Don't add new colors, font sizes or spacing units.
  - Don't use Unicode emoji in UI or code. Use monochrome inline SVG or plain text.
  - Keep Fira Code as the primary UI font and dark as the default theme; light-mode work goes through the theme system.
  - Extend existing widgets rather than adding parallel ones.

## Tests

`tests/TESTING_STANDARD.md` holds the rules and `tests/README.md` documents the helpers. Read them before changing `tests/helpers/`.

- Assert behavior, not source text. `read_text()` or AST assertions are acceptable only when the behavior can't be driven at runtime, and the docstring must say why.
- Never mutate `sys.modules`, `os.environ` or the CWD without cleanup. Use `monkeypatch` or `tests.helpers.import_state`.
- Use `tests.helpers.sqlite_db.make_temp_sqlite` for a file-backed DB. Keep the root `conftest.py` minimal.
- Don't add `skip` or `xfail`, or weaken assertions, to get a test passing. Mark a test `slow` only with evidence from `--durations`.
- JS logic is tested from `*_js.py` wrappers that run `node` as a subprocess and skip when `node` is absent.

## Contributing rules

- `dev` is the default branch and every PR targets it. `main` is curated by the maintainer.
- **This checkout is a personal fork (`origin` = janeiaraujo/odysseus) developing J.A.R.V.I.S. on `jarvis/dev`.** Never open PRs against, push to, or auto-merge from `upstream`; its push URL is deliberately `DISABLED`. The fork is public, so never commit secrets or personal data.
- Commits and PR titles use Conventional Commits, `type(scope): summary`; CI checks PR titles and descriptions.
- Keep PRs small, with one kind of change each. Don't mix file moves with behavior changes.
- The PR template requires a linked issue, reviewer test steps, and confirmation that you ran the app; tests alone don't count. UI changes need a screenshot from the running app, plus a mobile one if mobile is affected.
