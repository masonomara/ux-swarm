# UX Swarm — Web Architecture

## Services

| Service                           | Stack               | Host                       |
| --------------------------------- | ------------------- | -------------------------- |
| Frontend                          | Next.js             | Vercel                     |
| API                               | FastAPI (Python)    | Railway                    |
| Worker                            | Python + Playwright | Railway (separate service) |
| Database, Realtime, Auth, Storage | Supabase            | Supabase                   |
| Payments                          | Stripe              | —                          |

---

## Data Flow

```
1. User authenticates (Supabase Auth)

2. User fills form:
   - URL mode: enters URL + task + config
   - Screenshot mode: uploads image → Supabase Storage (presigned URL, direct from browser)

3. Next.js calls POST /jobs on Railway API:
   - API checks auth (Supabase JWT) + credits
   - API creates job row in Supabase (status: queued)
   - API deducts 1 credit from users.credits
   - API fire-and-forgets HTTP request to Worker (Railway private network)
   - API returns job_id

4. Frontend subscribes to job row via Supabase Realtime

5. Worker receives job, runs run_screenshot_swarm or run_browser_swarm:
   - on_agent_done callback → writes progress to Supabase (status: running, progress: n/total)
   - on completion → writes SwarmResult JSON to job row (status: complete)

6. Supabase Realtime pushes row update → frontend renders results live
```

---

## Database (Supabase Postgres)

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
  target        text        -- Supabase Storage URL or web URL
  task          text
  config        jsonb       -- { users, model, max_steps, viewport }
  progress      int         -- agents complete so far
  total         int         -- total agents in swarm
  result        jsonb       -- SwarmResult (written on completion)
  created_at    timestamptz default now()
  completed_at  timestamptz
```

---

## Railway Services

**ux-swarm-api** (FastAPI)

- Validates Supabase JWT on every request
- `POST /jobs` — check credits, create job row, forward to worker, return job_id
- `GET /jobs/{id}` — direct Supabase read (fallback if Realtime fails)
- `POST /webhooks/stripe` — top up credits on successful payment
- Lightweight, no Playwright, minimal RAM

**ux-swarm-worker** (Python + Playwright, same repo, separate Dockerfile entrypoint)

- Listens on Railway private network only (not public)
- `POST /run` — receives job, runs swarm, writes to Supabase
- Global `asyncio.Semaphore(3)` — hard cap on concurrent Playwright contexts
- Must use Dockerfile (not Nixpacks)
- Chromium flags: `--disable-dev-shm-usage --no-sandbox --disable-gpu`
- Railway Hobby plan: 8 GB RAM per replica (safe for 3 concurrent contexts)

---

## Storage (Supabase)

- Screenshot uploads go directly from the browser to Supabase Storage via presigned URL
- Next.js API route generates the presigned URL, returns it to the client
- Client uploads, gets back the public/signed URL, includes it in `POST /jobs`
- Bucket: `screenshots`, access: private (signed URLs only)

---

## Auth (Supabase Auth)

- Supabase Auth handles signup/login (email or OAuth)
- JWT passed as `Authorization: Bearer <token>` to Railway API
- Railway API verifies JWT against Supabase's public JWKS endpoint
- Row-level security on Supabase tables (users can only read their own jobs)

---

## Payments (Stripe)

- User buys a credit pack via Stripe Checkout (e.g. 10 runs / 50 runs / 200 runs)
- Stripe webhook → `POST /webhooks/stripe` → increment `users.credits`
- `POST /jobs` checks `users.credits > 0` before proceeding, returns 402 if zero
- No subscription for v1 — pay-as-you-go credit packs only

---

## Build Order

1. **Worker service** — wrap `run_browser_swarm` / `run_screenshot_swarm` in a FastAPI endpoint, write progress + result to Supabase
2. **API service** — job creation, auth check, credit check, forward to worker
3. **Next.js frontend** — form, Supabase Realtime subscription, results display
4. **Auth** — Supabase Auth (email login to start)
5. **Stripe** — credit packs, webhook, credit gate on job creation

---

## What Doesn't Change

The entire `swarm.py`, `agent.py`, `browser_agent.py`, `models.py`, and `personas.py` are used as-is. The web layer is purely additive — no changes to core swarm logic.
