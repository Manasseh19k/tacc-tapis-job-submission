# TACC Tapis Portal — Architecture

A Gen-UI conversational assistant for TACC's Tapis platform. Users log in with
their Tapis (TACC) identity, chat with an agent, and the agent helps them browse
systems/files, discover apps, and **submit and monitor HPC jobs**, with a
human-in-the-loop approval step (an inline card with a "Run Job" button) before
anything that consumes allocation. It also answers Tapis/HPC documentation
questions via retrieval-augmented generation (RAG).

Everything runs as the **logged-in user's own Tapis token**, so Tapis enforces
that user's real permissions on every call. There is no service account.

## Repositories

One git repository with two ends of the project (backend and frontend):

| Repo                      | Stack                                                                     | Role                                                                            |
| ------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `tacc-portal-backend`     | Python 3.13, FastAPI, Pydantic AI, LiteLLM, tapipy, ChromaDB (uv-managed) | The AG-UI agent service. Owns Tapis API calls, the agent/tools, and RAG.        |
| `tacc-portal-copilot-app` | Next.js 16, React 19, CopilotKit v2 (uv/npm)                              | The web UI: Tapis OAuth login, the chat surface, and the Gen-UI approval cards. |

## Request flow

```
browser
  │  Tapis OAuth2 login (authorization-code grant); access token stored in an
  │  httpOnly cookie by the Next.js /auth routes.
  ▼
Next.js  app/api/copilotkit/route.ts
  │  reads the httpOnly Tapis cookie; 401 if absent.
  │  CopilotRuntime -> HttpAgent POST to the backend, forwarding header
  │  X-Tapis-Token: <jwt>.
  ▼
FastAPI  app/main.py   POST /
  │  security.get_current_user   -> verify the JWT (signature, exp, iss, tenant, token_type)
  │  tapis_client.build_user_client -> per-request tapipy client bound to that token
  │  deps.AgentDeps               -> user + client + shared vector store
  ▼
_ApprovalReconcilingAGUIAdapter.dispatch_request(agent=..., deps=...)
  │  streams AG-UI events (SSE) back to CopilotKit
  ▼
agent tools -> Tapis API (as the user)  /  Chroma (unauthenticated docs retrieval)
```

For an approval-gated action (submit/cancel), the run _ends_ with a pending
approval; the browser renders a card; the user's decision comes back as an AG-UI
`resume[]` and the agent finishes the tool call. See "Human-in-the-loop" below.

## Backend modules (`tacc-portal-backend/app/`)

All modules are **implemented** (no remaining stubs). Status column notes how far
each is verified.

| Module               | Responsibility                                                                                                                                                                                                                                                                                     | Status                                                                               |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `config.py`          | `Settings` (pydantic-settings). The only reader of the environment. Loads `etc/.env` then `.env`. Validates the LLM endpoint (a custom `base_url` requires `openai_api_key`).                                                                                                                      | Done                                                                                 |
| `security.py`        | Auth boundary. Verifies the forwarded Tapis JWT with the tenant's RS256 public key (cached, TTL, refreshed on signature failure); checks `exp`/`iss`/tenant/`token_type`. Yields `AuthenticatedUser` (redacted repr). `AuthError` for 401s.                                                        | Done, verified live                                                                  |
| `tapis_client.py`    | `build_user_client(user)` -> a tapipy `Tapis` bound to the user's token (no login, no service account). `summarize_tapis_error` maps tapipy exceptions to short, user-safe strings. `redact` scrubs credential-bearing keys before logging. `TapisClientError`.                                    | Done, verified live                                                                  |
| `deps.py`            | `AgentDeps` -> carries the per-request `user`, `tapis` client, and shared `knowledge` store into tools via `RunContext`. No shared per-user state.                                                                                                                                                 | Done                                                                                 |
| `tools/files.py`     | `list_systems` (uses `listType="ALL"`), `list_files` (returns `FileListing` with a `truncated` flag), `read_file` (byte-capped, binary-detecting).                                                                                                                                                 | Done; `list_systems` verified live                                                   |
| `tools/jobs.py`      | Apps + Jobs tools: `list_apps` (`listType="ALL"`), `describe_app`, `validate_job_spec`, `submit_job`, `submit_job_request` (raw-JSON path), `build_job_request`, `get_job_status`, `list_jobs`, `get_job_output`, `cancel_job`. `submit_job`/`submit_job_request`/`cancel_job` are approval-gated. | Done; structured submit verified live (real job UUID). Cancel + raw-JSON unit-tested |
| `tools/knowledge.py` | `search_documentation` —> RAG retrieval tool. Filters at `_MIN_SCORE`, degrades to `found=False` on empty/failure.                                                                                                                                                                                 | Done, verified against live endpoint                                                 |
| `rag/store.py`       | `VectorStore` over Chroma (cosine). `_SingleInputOpenAIEmbeddingFunction` embeds one string per request (endpoint requires it — see gotchas). E5 query-instruction prefix applied query-side.                                                                                                      | Done, verified                                                                       |
| `rag/ingest.py`      | Offline pipeline + CLI: load (`.md/.txt/.html`; `.pdf` optional), fetch (httpx+bs4), structure-aware chunk (tiktoken), stable ids, upsert. Idempotent.                                                                                                                                             | Done, verified                                                                       |
| `agent.py`           | `build_model` (LiteLLM), `build_agent` (tools, `output_type=[str, DeferredToolRequests]`, `retries=2`), `_surface_tapis_errors` wrapper, system prompt.                                                                                                                                            | Done, verified live                                                                  |
| `main.py`            | FastAPI app, lifespan (prime tenant key, open vector store, build agent), `POST /` agent endpoint, `GET /health`, `_ApprovalReconcilingAGUIAdapter`, `AuthError`→401 / `TapisClientError`→502 handlers.                                                                                            | Done, verified live                                                                  |

## Frontend modules (`tacc-portal-copilot-app/`)

| Path                                    | Responsibility                                                                                                                                                                         |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lib/tapis.ts`                          | OAuth2 helpers: build authorize URL, exchange code for token, revoke, fetch userinfo; cookie names. (Refresh-token handling was deliberately removed — expired token forces re-login.) |
| `app/auth/login/route.ts`               | Step 1: random `state` (CSRF) in an httpOnly cookie, redirect to Tapis authorize.                                                                                                      |
| `app/auth/callback/route.ts`            | Step 2: verify `state`, exchange code, store access token in an httpOnly cookie.                                                                                                       |
| `app/auth/logout/route.ts`              | Revoke token, clear cookie.                                                                                                                                                            |
| `app/api/me/route.ts`                   | Returns the logged-in Tapis user's profile (or 401).                                                                                                                                   |
| `app/api/copilotkit/route.ts`           | CopilotKit runtime. Reads the Tapis cookie (401 if absent), forwards it as `X-Tapis-Token` to the backend `HttpAgent`.                                                                 |
| `proxy.ts`                              | Next.js 16 auth gate (formerly "middleware"): no token cookie → redirect to `/auth/login`. `/auth` and `/api/copilotkit` are excluded.                                                 |
| `app/layout.tsx`                        | `<CopilotKit>` provider (`agent="my_agent"`, `runtimeUrl="/api/copilotkit"`).                                                                                                          |
| `app/page.tsx`                          | Home page; mounts `<CopilotPopup>` and the three interrupt cards.                                                                                                                      |
| `components/JobApprovalCard.tsx`        | Renders the `submit_job` interrupt (structured `JobSpec`) → Run Job / Cancel.                                                                                                          |
| `components/JobRequestApprovalCard.tsx` | Renders the `submit_job_request` interrupt (raw Tapis JSON) → Submit / Cancel.                                                                                                         |
| `components/CancelJobCard.tsx`          | Renders the `cancel_job` interrupt (uuid/name/status) → Confirm / Cancel.                                                                                                              |

## Human-in-the-loop (the load-bearing design)

Reading is reversible; submitting or cancelling a job is not. Those actions must
not fire because a model inferred intent. Enforcement is in code, not prompting:

1. `submit_job` / `submit_job_request` / `cancel_job` **raise
   `pydantic_ai.ApprovalRequired` themselves** (guarded by `if not
ctx.tool_call_approved`) — they are **not** registered with
   `requires_approval=True`. That wrapper would raise a _metadata-less_
   `ApprovalRequired()` before the body runs; raising it explicitly lets the
   full spec (or raw request, or resolved job uuid/name/status) ride along as
   `metadata`.
2. The agent's `output_type` includes `DeferredToolRequests` — required, or a
   pending approval has nowhere to surface.
3. The AG-UI adapter turns the pending call into an interrupt whose `metadata`
   the matching frontend card renders. Each card's `useInterrupt` `enabled`
   predicate keys on a distinct metadata shape (`app_id` / `job_request` /
   `job_uuid`), so exactly one card claims each interrupt. **There is no default
   interrupt UI in CopilotKit** — an unclaimed interrupt just hangs, so every
   approval-gated tool needs a card.
4. The user's decision returns as an AG-UI `resume[]` entry, mapped to
   `ToolApproved`/`ToolDenied` (deny-by-default). The tool body executes only
   on approval.

## Key invariants (do not break)

- **No service account.** Every Tapis call uses the user's forwarded token.
- **No shared per-user state.** The tapipy client and `AgentDeps` are built per
  request; the agent and vector store are shared but hold no user state.
- **Never log the JWT.** Route anything log-bound through `tapis_client.redact`.
- **Retrieval is unauthenticated by design.** The vector store holds only
  material any logged-in user may read. User-scoped data goes through the Files
  API with the user's token, never the vector store.
- **Tapis errors reach the model, not the user as a crash.** `agent.py`'s
  `_surface_tapis_errors` converts `TapisClientError` raised in a tool into
  `ModelRetry`, so the agent explains the failure conversationally.

## Hard-won integration constraints

These each broke the end-to-end flow for the development.

- **Frontend and backend must use the same Tapis tenant.** `TAPIS_TENANT_URL`
  in the frontend `.env.local` and backend `etc/.env` must match, or the backend
  rejects tokens ("Token belongs to tenant X, expected Y"). Both are currently
  `https://portals.tapis.io` (the OAuth client is registered under `portals`).
- **The OAuth callback port must match the running frontend.** `TAPIS_CALLBACK_URL`
  is registered on the Tapis client (port `3000`); run the frontend on 3000 or
  the post-login redirect 404s.
- **`pydantic-ai-litellm>=0.2.8`.** Earlier versions' streamed-response class
  lacks `provider_url`, which the installed `pydantic-ai-slim` marks abstract;
  streaming (what the AG-UI adapter does) then crashes.
- **CopilotKit `agents` must be a plain object, not a factory function** in
  `app/api/copilotkit/route.ts` — the runtime does `Object.keys(agents)`; a
  function yields `[]` -> "No default agent provided".
- **Approval resume needs `_ApprovalReconcilingAGUIAdapter`** (`main.py`).
  CopilotKit's `useInterrupt` both injects a `role="tool"` message AND sends
  `resume[]`; Pydantic AI then sees a tool-return for a call it's also resuming
  and raises "already executed". The subclass drops the injected tool-return for
  resumed ids so only the resume approval applies.
- **The embedding endpoint takes a single string, not an array.** The TACC
  gateway routes `E5-Mistral-7B-Instruct` to SambaNova, whose embeddings API
  rejects a batched array `input`. `rag/store.py` uses
  `_SingleInputOpenAIEmbeddingFunction` (one request per string). Don't revert to
  the batching function.

## Configuration

**Backend** (`tacc-portal-backend/etc/.env`; also reads `.env`). Field names are
the uppercased `Settings` attributes:

| Var                          | Default                  | Notes                                                                   |
| ---------------------------- | ------------------------ | ----------------------------------------------------------------------- |
| `TAPIS_TENANT_URL`           | `https://tacc.tapis.io`  | Must match the frontend tenant. Set to `https://portals.tapis.io`.      |
| `BASE_URL`                   | (none)                   | OpenAI-compatible inference endpoint (chat and embeddings).             |
| `OPENAI_API_KEY`             | (none)                   | Required when `BASE_URL` is set.                                        |
| `LLM_MODEL`                  | `gpt-oss-120b`           | Chat model (LiteLLM).                                                   |
| `EMBEDDING_MODEL`            | `E5-Mistral-7B-Instruct` | Must match at ingest and query time.                                    |
| `LLM_PROVIDER`               | `openai`                 | LiteLLM `custom_llm_provider`.                                          |
| `CHROMA_PERSIST_DIR`         | `./my_chroma_db`         | Vector store on disk (gitignored).                                      |
| `CHROMA_COLLECTION`          | `documentation`          |                                                                         |
| `REQUIRE_TOKEN_VERIFICATION` | `true`                   | Never run `false` in production.                                        |
| `ALLOWED_ORIGINS`            | (empty)                  | Comma-separated; CORS is off by default (frontend proxies server-side). |

**Frontend** (`tacc-portal-copilot-app/.env.local`):
`TAPIS_TENANT_URL`, `TAPIS_CLIENT_ID`, `TAPIS_CLIENT_KEY`, `TAPIS_CALLBACK_URL`
(`http://localhost:3000/auth/callback`), `APP_POST_LOGIN_PATH`, and optional
`FASTAPI_URL` (defaults to `http://127.0.0.1:8000`).

Neither `.env` file is committed.

Populate the knowledge base (optional; docs Q&A returns "I don't know" until it is populated, the app runs fine without it):

```bash
uv run python -m app.rag.ingest --url https://tapis.readthedocs.io/en/latest/technical/jobs.html --reset
# or:  uv run python -m app.rag.ingest --source ./docs
```

`GET /health` reports `agent_ready`, `knowledge_enabled`, and `knowledge_chunks`
(0 = ingestion never ran).

## What's left / next steps

- **Browser-verify the cancel and raw-JSON submit flows**
- (implemented; the structured submit is confirmed live and shares the same resume path).
- **Tune RAG on a real corpus:** `_MIN_SCORE` (0.45, set from a tiny sample) and
  `chunk_document` sizes can validate against real Tapis docs and questions.
- **`pypdf`** is an optional dependency — `uv add pypdf` if PDF sources are needed.
- **`JobSpec` is intentionally flat.** The raw-JSON path (`build_job_request` and
  `submit_job_request`) is the escape hatch for fields it can't express
  (container args, env vars, scheduler options, file-input arrays, subscriptions).
