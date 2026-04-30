# UX Swarm — Product Engineering Research

## 1. What the CLI Does

ux-swarm is a synthetic UX testing tool. It dispatches a configurable swarm of AI agents — each embodying a distinct user persona — to attempt a plain-language task on either a live URL or a static screenshot/mockup. The result is a statistically meaningful completion rate, a ranked list of friction points, and a per-persona breakdown, all without recruiting real users.

**Two modes:**

| Mode | Input | LLM Calls | What an Agent Does |
|---|---|---|---|
| Screenshot | Image file / path | 1 per agent | Analyzes the image, decides the target element, reports friction |
| Browser | URL | Up to `max_steps` per agent | Navigates live site step-by-step via Playwright; clicks, types, scrolls |

The tool outputs: completion rate ± margin of error, per-persona breakdown, canonical friction points (LLM-deduplicated), total LLM cost, and an append-only local results log.

---

## 2. CLI Architecture (Current State)

### Entry Point

Registered as both `swarm` and `ux-swarm` via `pyproject.toml` scripts.

```
swarm [command] [args]
ux-swarm [command] [args]
```

`SmartGroup` intercepts bare URL or image-path arguments and automatically prepends `run`, so `swarm example.com "sign up"` works without typing `run`.

### Commands

| Command | Purpose |
|---|---|
| `run <target> <task> [options]` | Core command — runs the swarm |
| `config` | Interactive setup wizard (provider → API key → model → Playwright) |
| `users [--edit]` | List personas; `--edit` writes a customizable `users.json` |
| `results [-n N]` | Display table of saved runs |
| `expand` | Per-agent detail for the most recent run |
| `help [command]` | Show help |

### Source File Map

| File | Lines | Role |
|---|---|---|
| `main.py` | 781 | CLI commands, live display, result persistence |
| `browser_agent.py` | 388 | Single browser agent (Playwright + LLM loop) |
| `config.py` | 358 | Config wizard, provider auth, model listing |
| `cli.py` | 265 | `SmartGroup`, `SwarmCommand`, `CliError` |
| `swarm.py` | 271 | Orchestration, concurrency, aggregation |
| `agent.py` | 124 | Single screenshot agent |
| `models.py` | 69 | Pydantic schemas |
| `personas.py` | 64 | Persona loading, weighted distribution |
| `menu.py` | 72 | Arrow-key terminal menu |
| **Total** | **2,392** | |

### Data Flow (Browser Mode)

```
swarm https://example.com "sign up for an account"
  → SmartGroup detects URL → routes to run command
  → load config (.swarm/config.json + ~/.config/ux-swarm/config.json)
  → load personas (.swarm/users.json or defaults)
  → distribute_users(personas, n=20) → weighted persona assignment per agent
  → launch Playwright chromium (headless)
  → asyncio.TaskGroup spawns 20 concurrent tasks:
      each task: browser context → navigate → LLM loop (max 8 steps):
        extract interactive elements → take screenshot → build prompt
        → LLM call (llm_semaphore, max 3 concurrent) → parse BrowserStep JSON
        → execute action (click/type/scroll/etc.) → track friction
        → stop on done/give_up
  → consolidate friction points (one LLM deduplication call)
  → aggregate: completion_rate, margin_of_error, per-user breakdown, avg_steps
  → append to .swarm/results.json
  → rich terminal display
```

### Concurrency Model

Two independent semaphores:
- `browser_sem`: Limits concurrent Playwright contexts (default 5, configurable)
- `llm_sem`: Limits concurrent LLM calls (hardcoded 3, litellm limitation)

### Pydantic Models (Core Schemas)

```
UserType           persona label, weight, description, accessibility flag
ScreenshotDecision LLM response for screenshot mode
BrowserStep        LLM response for one browser step (action, element_index, friction, success)
AgentResult        Output of one agent (status, steps, cost, friction, actions, URLs)
SwarmResult        Aggregated output (completion_rate, margin_of_error, breakdown, friction_points)
```

### Config Layering

```
~/.config/ux-swarm/config.json   ← global defaults
.swarm/config.json               ← project-level overrides (wins)
```

Config keys: `provider`, `api_key`, `model`.

### Data Persistence

All in `.swarm/` relative to the working directory:

```
.swarm/
  config.json     — LLM provider + API key + model
  users.json      — custom personas (optional; auto-created by swarm users --edit)
  results.json    — append-only log of every SwarmResult
```

---

## 3. The Target Architecture (from `.docs/ARCHITECTURE.md`)

The planned product wraps this CLI engine in a full web service. Core principle: **the swarm logic (`swarm.py`, `agent.py`, `browser_agent.py`, `models.py`, `personas.py`) is used entirely as-is.** The web layer is purely additive.

### Service Topology

| Service | Stack | Host | Role |
|---|---|---|---|
| Frontend | Next.js | Vercel | Form, auth UI, real-time results display |
| API | FastAPI | Railway | Auth check, credit check, job creation, Stripe webhook |
| Worker | FastAPI + Playwright | Railway (separate service) | Runs the actual swarm, writes progress to Supabase |
| Database / Realtime / Auth / Storage | Supabase | Supabase | Postgres, JWT auth, websocket push, file storage |
| Payments | Stripe | — | Pay-as-you-go credit packs |

### Data Model (Supabase Postgres)

```sql
users
  id                  uuid primary key  -- matches Supabase Auth uid
  email               text
  credits             int default 0
  stripe_customer_id  text

jobs
  id            uuid primary key default gen_random_uuid()
  user_id       uuid references users
  status        text        -- queued | running | complete | failed
  mode          text        -- screenshot | browser
  target        text        -- URL or Supabase Storage URL
  task          text
  config        jsonb       -- { users, model, max_steps, viewport }
  progress      int
  total         int
  result        jsonb       -- SwarmResult written on completion
  created_at    timestamptz default now()
  completed_at  timestamptz
```

### Request Lifecycle

```
1. User authenticates → Supabase Auth JWT
2. Frontend POST /jobs (Railway API):
   - Verify JWT
   - Check users.credits > 0 → 402 if zero
   - Deduct 1 credit
   - INSERT job row (status: queued)
   - Fire-and-forget POST /run to Worker (Railway private network)
   - Return { job_id }
3. Frontend subscribes to job row via Supabase Realtime
4. Worker runs swarm:
   - on_agent_done → UPDATE job SET progress=n, status='running'
   - on complete → UPDATE job SET result=SwarmResult, status='complete', completed_at=now()
5. Supabase Realtime pushes row update → frontend re-renders live
```

### Worker Design Constraints

- Private network only (not reachable from internet)
- `asyncio.Semaphore(3)` global cap on concurrent Playwright contexts
- Must use Dockerfile (not Nixpacks) — Playwright requires system deps
- Chromium flags: `--disable-dev-shm-usage --no-sandbox --disable-gpu`
- Railway Hobby plan: 8 GB RAM — safe for 3 concurrent browser contexts

### API Design

**`ux-swarm-api`** (FastAPI, no Playwright, minimal RAM):

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | Create job, deduct credit, forward to worker |
| `GET /jobs/{id}` | Direct Supabase read (Realtime fallback) |
| `POST /webhooks/stripe` | Top up credits on payment |

**`ux-swarm-worker`** (FastAPI + Playwright):

| Endpoint | Purpose |
|---|---|
| `POST /run` | Receive job payload, run swarm, write progress + result to Supabase |

### Storage (Screenshots)

- Upload goes directly from browser → Supabase Storage via presigned URL
- Next.js API route generates presigned URL
- Client uploads, gets signed URL, sends URL in `POST /jobs`
- Bucket: `screenshots`, access: private

### Payments (Stripe)

- Pay-as-you-go credit packs (no subscriptions in v1)
- Stripe webhook → `POST /webhooks/stripe` → `UPDATE users SET credits = credits + N`
- `POST /jobs` gates on `credits > 0`

### Build Order

1. **Worker** — wrap `run_browser_swarm` / `run_screenshot_swarm` in FastAPI endpoint; write progress + result to Supabase
2. **API** — job creation, auth check, credit check, forward to worker
3. **Next.js frontend** — form, Realtime subscription, results display
4. **Auth** — Supabase Auth (email login first)
5. **Stripe** — credit packs, webhook, credit gate

---

## 4. Reference Architecture: ArjanCodes FastAPI Example

Source: `https://github.com/ArjanCodes/examples/tree/main/2025/project`

This is a clean, educational FastAPI project with a 4-layer architecture. It's the closest publicly available pattern to what the ux-swarm API + Worker need to implement.

### Directory Structure

```
app/
  main.py              FastAPI entry point (logging, table creation, router registration)
  api/v1/user.py       HTTP endpoints (presentation layer)
  services/user_service.py  Business logic (service layer)
  db/schema.py         SQLAlchemy ORM models + engine + session factory (data layer)
  models/user.py       Pydantic schemas (UserCreate / UserRead)
  core/config.py       Pydantic Settings BaseSettings
  core/logging.py      Logging setup
tests/
  test_db.py           In-memory SQLite fixture
  api/v1/test_user.py  Integration tests with dependency override
```

### Key Patterns to Adopt

**1. Layered separation**

```
Presentation  (api/v1/)       → HTTP, routing, status codes, serialization
Service       (services/)     → business logic, orchestration
Data          (db/)           → ORM models, session management
Schema        (models/)       → Pydantic I/O contracts
Config        (core/config.py) → Pydantic Settings, env var injection
```

**2. Dependency injection via `Depends()`**

```python
def get_user_service(db: Session = Depends(lambda: SessionLocal())) -> UserService:
    return UserService(db)

@router.post("/", response_model=UserRead)
def create_user(user_create: UserCreate, service: UserService = Depends(get_user_service)):
    return service.create_user(user_create)
```

The service is constructed per-request; tests override `get_user_service` via `app.dependency_overrides`.

**3. Config via Pydantic Settings**

```python
class Config(BaseSettings):
    db_user: str = ""
    db_password: str = ""
    db_name: str = "test.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///./{self.db_name}"

config = Config()
```

Environment variables automatically override field values (case-insensitive). `.env` files loaded via `dotenv`.

**4. Input / Output model split**

```python
class UserCreate(BaseModel):   # request body
    name: str

class UserRead(BaseModel):     # response
    id: int
    name: str
```

Never expose ORM objects directly in responses.

**5. Service returns None; router raises HTTPException**

```python
# service
def get_user(self, user_id: int) -> User | None:
    return self._db.query(User).filter(User.id == user_id).first()

# router
user = service.get_user(user_id)
if not user:
    raise HTTPException(status_code=404, detail="User not found")
```

**6. Dependency override for tests**

```python
def override_get_user_service():
    return UserService(TestingSessionLocal())

app.dependency_overrides[get_user_service] = override_get_user_service
```

In-memory SQLite with `StaticPool` ensures test isolation without hitting a real database.

**7. API versioning**

All routes at `/api/v1/...` prefix — enables parallel v2 deployment later.

**8. App initialization sequence**

```python
app = FastAPI(title="...")
setup_logging()
Base.metadata.create_all(bind=engine)
app.include_router(user_router, prefix="/api/v1")
```

---

## 5. Key Engineering Decisions for the Worker

The Worker is the most critical and complex service to build. These are the decisions that will shape its implementation.

### 5.1 Mapping CLI callbacks to Supabase writes

The swarm engine already has two callbacks:

```python
on_agent_done(done_count: int, total: int, result: AgentResult) -> None
on_agent_step(agent_id: int, status: str, detail: str, step: int) -> None
```

The Worker wraps these to write progress into Supabase:

```python
async def on_agent_done(done, total, result):
    await supabase.table("jobs").update({
        "progress": done,
        "status": "running",
    }).eq("id", job_id).execute()

async def on_completion(swarm_result):
    await supabase.table("jobs").update({
        "status": "complete",
        "result": swarm_result.model_dump(),
        "completed_at": datetime.now(UTC).isoformat(),
    }).eq("id", job_id).execute()
```

Note: `run_browser_swarm` and `run_screenshot_swarm` accept these callbacks. No changes to swarm engine required.

### 5.2 Worker FastAPI endpoint signature

```python
class JobPayload(BaseModel):
    job_id: str
    mode: Literal["browser", "screenshot"]
    target: str
    task: str
    config: JobConfig  # users, model, max_steps, viewport

class JobConfig(BaseModel):
    users: int = 20
    model: str
    max_steps: int = 8
    viewport: int = 1280

@app.post("/run")
async def run_job(payload: JobPayload, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_swarm, payload)
    return {"accepted": True}
```

Using `BackgroundTasks` means the HTTP response returns immediately (non-blocking to the API service). The actual swarm runs in the background.

### 5.3 Global semaphore in the Worker

```python
# module level
_BROWSER_SEM = asyncio.Semaphore(3)

async def execute_swarm(payload: JobPayload):
    async with _BROWSER_SEM:
        result = await run_browser_swarm(...)
```

This caps total concurrent Playwright contexts across all simultaneous job requests. The `_LLM_CONCURRENCY = 3` cap inside `swarm.py` handles LLM calls per-swarm internally.

### 5.4 Error handling and job status

Worker must catch all exceptions and write `status: failed` to Supabase — otherwise the frontend hangs waiting for a Realtime update that never comes.

```python
try:
    result = await run_browser_swarm(...)
    await write_result(job_id, result)
except Exception as e:
    await write_failure(job_id, str(e))
```

### 5.5 Screenshot mode: Supabase Storage URL

For screenshot jobs, `target` is a Supabase Storage signed URL (not a local file path). The `agent.py` `_load_image` function currently loads from disk. Two options:

1. **Preferred**: Download the image from the signed URL to a temp file, then call the existing `run_screenshot_swarm()` with the temp path.
2. Alternative: Extend `_load_image` to accept HTTP URLs directly.

Option 1 keeps the core engine unchanged.

### 5.6 Model/API key injection

The Worker receives `model` in the job config. The API key for that provider lives in the Worker's environment variables (set via Railway env vars, not user-submitted). The Worker calls the equivalent of `_inject_api_key(provider, api_key)` at startup or per-job based on model prefix.

---

## 6. Key Engineering Decisions for the API

### 6.1 Auth: Supabase JWT verification

Every API endpoint (except Stripe webhook) must verify the Supabase JWT:

```python
from jose import jwt

async def get_current_user(authorization: str = Header(...)):
    token = authorization.removeprefix("Bearer ")
    payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"])
    return payload["sub"]  # user UUID
```

Alternatively use the Supabase Python client to verify against the JWKS endpoint.

### 6.2 Credit atomicity

Credit deduction must be atomic to prevent double-spend:

```sql
UPDATE users SET credits = credits - 1
WHERE id = $user_id AND credits > 0
RETURNING credits
```

If `RETURNING` returns no row, return 402. Use a Supabase RPC (Postgres function) to make this one round-trip.

### 6.3 Fire-and-forget to Worker

The API must not wait for the Worker to finish. Use `httpx.AsyncClient` with a short timeout on the POST to Worker, treating a successful receipt (HTTP 200/202) as confirmation the job was accepted:

```python
async with httpx.AsyncClient() as client:
    await client.post(
        f"{WORKER_INTERNAL_URL}/run",
        json=payload.model_dump(),
        timeout=5.0,
    )
```

Network errors here are retried or the job is marked failed immediately.

### 6.4 Stripe webhook verification

```python
@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    if event.type == "checkout.session.completed":
        # increment credits
```

Always verify the Stripe signature. Never trust the raw payload.

---

## 7. Differences Between ArjanCodes Pattern and UX Swarm Needs

| Concern | ArjanCodes Example | UX Swarm Worker/API |
|---|---|---|
| Database | SQLAlchemy ORM (SQLite) | Supabase Python client (Postgres via HTTP) |
| Session management | `SessionLocal()` per request | Supabase client is stateless HTTP; no sessions |
| Background work | Synchronous, request-scoped | Long-running async tasks (seconds to minutes) |
| Auth | None | Supabase JWT verification on every request |
| Response pattern | Synchronous result | Async job: accept → job_id → Realtime update |
| Testing | In-memory SQLite override | Mock Supabase client; mock swarm engine |
| Service layer | CRUD over ORM | Orchestration over async swarm functions |
| Deployment | Docker (basic) | Railway Dockerfile, Chromium flags, env vars |

The ArjanCodes layering (config → models → services → api) is directly applicable, with Supabase replacing SQLAlchemy and `BackgroundTasks` replacing synchronous handlers.

---

## 8. What Doesn't Change

Per the architecture document, the entire core engine is used as-is:

- `swarm.py` — orchestration and aggregation
- `agent.py` — screenshot agent
- `browser_agent.py` — browser agent
- `models.py` — Pydantic schemas (including `SwarmResult`, which is written directly to Supabase `result` jsonb column)
- `personas.py` — persona loading and distribution

The Worker simply imports and calls `run_browser_swarm()` / `run_screenshot_swarm()` with Supabase-writing callbacks.

---

## 9. Open Questions / Decisions to Resolve

1. **Screenshot temp file strategy**: Where are temp files written in the Worker container, and are they cleaned up after each job?

2. **LLM API keys**: Are all provider keys baked into Worker env vars, or does the user supply their own key? If user-supplied, where is it stored securely?

3. **Worker scaling**: Railway auto-scales replicas under load. With a global `asyncio.Semaphore(3)`, each replica independently caps at 3 contexts. Under high load, multiple replicas run in parallel — this is correct behavior, but must be documented.

4. **Supabase RLS**: Row-level security must ensure users can only read their own jobs. This requires `auth.uid() = user_id` policies on the `jobs` table.

5. **Job timeout**: What happens if an agent loop hangs? The Worker needs a per-job wall-clock timeout (e.g., 5 minutes) to prevent zombie tasks holding the semaphore.

6. **Persona management in web version**: Currently personas live in `.swarm/users.json`. In the web product, should personas be per-user (stored in Supabase) or global defaults with per-job overrides?

7. **Result storage size**: `SwarmResult` with 20 agents, each with full action history and screenshots, can be large. The `result` jsonb column should store a summarized version; full per-agent detail could go to Supabase Storage.

8. **Credit pricing**: How many credits per run? Does browser mode cost more than screenshot mode (it's significantly more expensive in LLM calls)?
