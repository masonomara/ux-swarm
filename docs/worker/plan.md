# Worker Service — Implementation Plan

## What we're building

A FastAPI service (`worker/`) that wraps `run_browser_swarm` and `run_screenshot_swarm`
in an HTTP endpoint, writes live progress and final results to Supabase, and deploys as a
separate Railway service from the same repo. The existing swarm engine is **not modified**.

---

## Directory layout

```
ux-swarm/                        ← existing repo root
  src/ux_swarm/                  ← existing package, untouched
  worker/                        ← new
    Dockerfile
    app/
      main.py                    FastAPI app, lifespan, startup checks
      api/
        run.py                   POST /run
        health.py                GET /health
      models/
        job.py                   JobPayload, JobConfig Pydantic schemas
      services/
        executor.py              execute_swarm() orchestrator
        supabase.py              Supabase client + write helpers
      core/
        config.py                Pydantic Settings (env vars)
        logging.py               logging.basicConfig
  pyproject.toml                 add worker deps under [project.optional-dependencies]
```

Everything in `worker/app/` imports from `ux_swarm` directly — no duplication.

---

## Step 1 — Dependencies

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
worker = [
    "fastapi>=0.116",
    "uvicorn>=0.35",
    "supabase>=2.10",          # supabase-py async client
    "httpx>=0.28",
    "pydantic-settings>=2.10",
    "python-dotenv>=1.1",
]
```

Install locally with `pip install -e ".[worker]"`.

---

## Step 2 — Config (`worker/app/core/config.py`)

Use Pydantic Settings so Railway env vars are read automatically:

```python
from pydantic_settings import BaseSettings

class WorkerSettings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str     # service role — bypasses RLS for worker writes
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    max_concurrent_browsers: int = 3   # global cap across all jobs

settings = WorkerSettings()
```

Railway env vars to set:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` (whichever providers are offered)
- `MAX_CONCURRENT_BROWSERS` (optional, defaults to 3)

---

## Step 3 — Supabase client (`worker/app/services/supabase.py`)

```python
from supabase import AsyncClient, acreate_client
from worker.app.core.config import settings

_client: AsyncClient | None = None

async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = await acreate_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
    return _client

async def write_progress(job_id: str, progress: int, total: int) -> None:
    client = await get_client()
    await client.table("jobs").update(
        {"status": "running", "progress": progress, "total": total}
    ).eq("id", job_id).execute()

async def write_result(job_id: str, result_dict: dict) -> None:
    from datetime import datetime, timezone
    client = await get_client()
    await client.table("jobs").update({
        "status": "complete",
        "result": result_dict,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

async def write_failure(job_id: str, error: str) -> None:
    client = await get_client()
    await client.table("jobs").update(
        {"status": "failed", "result": {"error": error}}
    ).eq("id", job_id).execute()
```

Using the service role key means RLS is bypassed — the worker can write to any job row.
The API service uses the anon key + JWT, so it is subject to RLS.

---

## Step 4 — Job schemas (`worker/app/models/job.py`)

Mirrors what the API service will POST. Use the same field names as the `jobs` Supabase table.

```python
from typing import Literal
from pydantic import BaseModel

class JobConfig(BaseModel):
    users: int = 20
    model: str                  # e.g. "anthropic/claude-3-5-sonnet-20241022"
    max_steps: int = 8          # browser mode only
    viewport: int = 1280        # browser mode only
    max_concurrent: int = 5     # agents concurrent within this job

class JobPayload(BaseModel):
    job_id: str
    mode: Literal["browser", "screenshot"]
    target: str                 # URL (browser) or Supabase Storage signed URL (screenshot)
    task: str
    config: JobConfig
```

---

## Step 5 — Executor (`worker/app/services/executor.py`)

This is the core of the worker. It bridges three things:

1. The async swarm engine (`run_browser_swarm` / `run_screenshot_swarm`)
2. The sync callbacks the engine expects
3. Async Supabase writes

### The callback problem

Both `on_agent_done` and `on_agent_step` are typed as **sync** callables:

```python
# from swarm.py — actual signatures
on_agent_done: Callable[[int, int, AgentResult | None], None] | None = None
on_agent_step: Callable[[int, str, str, int], None] | None = None
```

These callbacks fire inside `_run_agent`, which is an `async` coroutine running in the
active event loop. We can schedule async work from them by calling
`asyncio.get_running_loop().create_task(coro)` — this enqueues the coroutine onto the
already-running loop without blocking the callback:

```python
def on_agent_done(done: int, total: int, result: AgentResult | None) -> None:
    loop = asyncio.get_running_loop()
    loop.create_task(write_progress(job_id, done, num_agents))
```

This is safe here because the callback is always invoked from an async context (inside
the TaskGroup in `swarm.py`). `create_task` is non-blocking — the Supabase write runs
concurrently with the next agents.

### Screenshot mode: temp file

`run_screenshot_agent` calls `_load_image(target)` which does `Path(target).read_bytes()`.
It only works with local paths. For screenshot jobs the `target` is a Supabase Storage
signed URL, so the Worker must download it first:

```python
import httpx
import tempfile
from pathlib import Path

async def _fetch_screenshot(url: str) -> Path:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30.0)
        resp.raise_for_status()
    suffix = Path(url.split("?")[0]).suffix or ".png"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.flush()
    return Path(tmp.name)
```

The local path is passed to `run_screenshot_swarm()` as `target`, then cleaned up in
`finally`.

### LLM API key injection

The Worker's environment already has `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. set by
Railway. `litellm` reads these env vars automatically — no extra injection step needed at
the worker level. The `_inject_api_key` helper in `main.py` is CLI-specific (it copies
from the `.swarm/config.json` into `os.environ`); the Worker skips it entirely.

### CliError handling

`_aggregate()` in `swarm.py` raises `CliError` (a `click.ClickException`) when all agents
fail. The Worker must catch this explicitly and write `status: failed`:

```python
from ux_swarm.cli import CliError

try:
    result = await run_browser_swarm(...)
except CliError as e:
    await write_failure(job_id, e.format_message())
    return
except Exception as e:
    await write_failure(job_id, str(e))
    return
```

### Full executor function

```python
import asyncio
import tempfile
from pathlib import Path

import httpx
from ux_swarm.cli import CliError
from ux_swarm.models import AgentResult, UserType
from ux_swarm.personas import load_users
from ux_swarm.swarm import run_browser_swarm, run_screenshot_swarm

from worker.app.models.job import JobPayload
from worker.app.services.supabase import write_failure, write_progress, write_result


async def execute_swarm(payload: JobPayload) -> None:
    job_id = payload.job_id
    cfg = payload.config
    num_agents = cfg.users

    # --- load personas (defaults if no custom users.json in cwd) ---
    users: list[UserType] = load_users()

    def on_agent_done(done: int, total: int, result: AgentResult | None) -> None:
        asyncio.get_running_loop().create_task(
            write_progress(job_id, done, total)
        )

    try:
        if payload.mode == "browser":
            swarm_result = await run_browser_swarm(
                url=payload.target,
                task=payload.task,
                users=users,
                num_agents=num_agents,
                model=cfg.model,
                max_concurrent=cfg.max_concurrent,
                max_steps=cfg.max_steps,
                viewport=cfg.viewport,
                headed=False,
                on_agent_done=on_agent_done,
            )

        else:  # screenshot
            tmp_path = await _fetch_screenshot(payload.target)
            try:
                swarm_result = await run_screenshot_swarm(
                    target=str(tmp_path),
                    task=payload.task,
                    users=users,
                    num_agents=num_agents,
                    model=cfg.model,
                    max_concurrent=cfg.max_concurrent,
                    on_agent_done=on_agent_done,
                )
            finally:
                tmp_path.unlink(missing_ok=True)

    except CliError as e:
        await write_failure(job_id, e.format_message())
        return
    except Exception as e:
        await write_failure(job_id, str(e))
        return

    await write_result(job_id, swarm_result.model_dump())
```

---

## Step 6 — Endpoint (`worker/app/api/run.py`)

```python
from fastapi import APIRouter, BackgroundTasks
from worker.app.models.job import JobPayload
from worker.app.services.executor import execute_swarm

router = APIRouter()

@router.post("/run", status_code=202)
async def run_job(payload: JobPayload, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_swarm, payload)
    return {"accepted": True, "job_id": payload.job_id}
```

`BackgroundTasks.add_task` starts `execute_swarm` after the response is sent.
The HTTP 202 response returns immediately so the API service is never left waiting.

---

## Step 7 — Global concurrency cap (`worker/app/main.py`)

The architecture specifies a hard cap of 3 concurrent Playwright contexts across all
simultaneous jobs. This is a **module-level semaphore** shared across requests.

The issue: `run_browser_swarm` accepts `max_concurrent` which is a per-job semaphore
controlling contexts within that job. We also need a cross-job cap.

**Solution**: wrap `execute_swarm` at the endpoint level with a module-level semaphore:

```python
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI

from worker.app.api import health, run
from worker.app.core.config import settings
from worker.app.core.logging import setup_logging

_GLOBAL_BROWSER_SEM: asyncio.Semaphore | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _GLOBAL_BROWSER_SEM
    _GLOBAL_BROWSER_SEM = asyncio.Semaphore(settings.max_concurrent_browsers)
    setup_logging()
    yield

app = FastAPI(title="ux-swarm-worker", lifespan=lifespan)
app.include_router(health.router)
app.include_router(run.router)
```

Expose the semaphore to the executor via a getter:

```python
# worker/app/main.py
def get_browser_semaphore() -> asyncio.Semaphore:
    assert _GLOBAL_BROWSER_SEM is not None
    return _GLOBAL_BROWSER_SEM
```

Then in `executor.py`, wrap browser jobs:

```python
from worker.app.main import get_browser_semaphore

async def execute_swarm(payload: JobPayload) -> None:
    ...
    if payload.mode == "browser":
        async with get_browser_semaphore():
            swarm_result = await run_browser_swarm(...)
    else:
        swarm_result = await run_screenshot_swarm(...)
```

Screenshot jobs do not hold the browser semaphore — they only consume LLM calls,
which `swarm.py` already caps via `_LLM_CONCURRENCY = 3`.

---

## Step 8 — Health endpoint (`worker/app/api/health.py`)

```python
from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health():
    return {"status": "ok"}
```

Railway uses this for health checks. Set the health check path to `/health` in the
Railway service config.

---

## Step 9 — Dockerfile (`worker/Dockerfile`)

Playwright requires system dependencies — Nixpacks cannot install them. Must use Dockerfile.

```dockerfile
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY worker/ worker/

RUN pip install --no-cache-dir -e ".[worker]"
RUN playwright install chromium --with-deps

EXPOSE 8000

CMD ["uvicorn", "worker.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

The `--with-deps` flag installs Chromium's OS-level deps inside the container so the
apt install above is actually redundant — but keeping both ensures no silent gaps.

---

## Step 10 — Supabase database setup

Run this SQL in the Supabase dashboard (or via migrations):

```sql
create table users (
  id                  uuid primary key references auth.users,
  email               text,
  credits             int default 0,
  stripe_customer_id  text
);

create table jobs (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid references users,
  status        text not null default 'queued',
  mode          text not null,
  target        text not null,
  task          text not null,
  config        jsonb not null default '{}',
  progress      int default 0,
  total         int default 0,
  result        jsonb,
  created_at    timestamptz default now(),
  completed_at  timestamptz
);

-- Row-level security: users see only their own jobs
alter table jobs enable row level security;

create policy "users read own jobs"
  on jobs for select
  using (auth.uid() = user_id);
```

The `users` table mirrors `auth.users` so the API can check credits in a single query.
The service role key used by the Worker bypasses RLS, allowing it to write to any job row.

---

## Step 11 — Personas in the Worker context

`load_users()` in `personas.py` reads from `.swarm/users.json` relative to the working
directory. In the Worker container, this file won't exist, so it falls back to
`DEFAULT_USERS` (a single "Default User" persona).

**For v1**: accept this — the default persona is sufficient for initial launch.

**For v2**: add a `personas` field to `JobConfig` (list of `UserType`-compatible dicts)
so the frontend can pass custom personas without needing a file on disk:

```python
class JobConfig(BaseModel):
    ...
    personas: list[dict] | None = None  # if None, use Worker defaults
```

In `executor.py`:

```python
if cfg.personas:
    users = [UserType.model_validate(p) for p in cfg.personas]
else:
    users = load_users()
```

This requires zero changes to the swarm engine.

---

## Build order for this service

1. `worker/app/core/config.py` — settings class
2. `worker/app/core/logging.py` — logging setup
3. `worker/app/services/supabase.py` — client + write helpers
4. `worker/app/models/job.py` — request schemas
5. `worker/app/services/executor.py` — swarm execution
6. `worker/app/api/run.py` — POST /run
7. `worker/app/api/health.py` — GET /health
8. `worker/app/main.py` — app, lifespan, semaphore
9. `worker/Dockerfile`
10. Create Supabase tables
11. Deploy to Railway, set env vars, verify `/health`
12. Smoke test: `POST /run` with a minimal payload, watch Supabase `jobs` row update live

---

## What does NOT change

These files are imported unchanged by the Worker:

| File                            | Reason unchanged                                                   |
| ------------------------------- | ------------------------------------------------------------------ |
| `src/ux_swarm/swarm.py`         | Callbacks are sync — we work around this in the executor           |
| `src/ux_swarm/agent.py`         | `_load_image` is local-file-only — we download to temp in executor |
| `src/ux_swarm/browser_agent.py` | No changes needed                                                  |
| `src/ux_swarm/models.py`        | `SwarmResult.model_dump()` serializes directly into `jobs.result`  |
| `src/ux_swarm/personas.py`      | Falls back to defaults if no users.json in container               |

---

## Key non-obvious decisions

**Why `BackgroundTasks` and not `asyncio.create_task` directly in the endpoint?**
FastAPI's `BackgroundTasks` are tied to the request lifecycle and run after the response
is sent. `asyncio.create_task` would work too, but `BackgroundTasks` is the FastAPI idiom
and integrates correctly with the ASGI lifecycle and exception handling.

**Why module-level semaphore and not per-job?**
The `max_concurrent` inside `run_browser_swarm` controls how many browser contexts run
_within_ a single job. The module-level semaphore controls how many jobs can hold browser
contexts simultaneously. Both are needed. On a Railway Hobby instance with 8 GB RAM,
3 concurrent Playwright contexts (across all jobs) is safe.

**Why service role key in the Worker?**
RLS on `jobs` allows users to read only their own rows. The Worker writes to job rows
belonging to any user — it needs to bypass RLS. The service role key is the standard
Supabase mechanism for server-side trusted writes. Never expose this key to the frontend.

**Why `asyncio.get_running_loop().create_task()` inside sync callbacks?**
The callbacks are sync (`Callable[..., None]`) and are always invoked from within an
async coroutine (`_run_agent` inside the TaskGroup). At that point the event loop is
running, so `get_running_loop()` is valid and `create_task()` schedules the Supabase
write concurrently without blocking the callback or the next agent step.

**Why download screenshot to a temp file rather than extending `_load_image`?**
Keeps the swarm engine's public API unchanged. The single-responsibility trade-off: the
executor handles I/O orchestration; the engine handles LLM reasoning. A future v2 could
accept a URL in `_load_image` if multiple callers need it.
