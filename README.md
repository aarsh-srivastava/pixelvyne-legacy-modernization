# PixelVyne Modernize

Agentic Oracle → PostgreSQL migration that proves the migrated code still behaves the
same. Upload legacy SQL, and the platform discovers objects and dependencies, converts
them (Ora2Pg → SQLGlot → Gemini), runs every object on **both** databases with the
same tests, loops failures back for repair, and reports the evidence.

Translation is a commodity; **verified functional equivalence is the product.**

## Roadmap

| Step | What | Status |
| --- | --- | --- |
| 1 | Machine prerequisites | on your laptops |
| 2 | Repository and Git setup | done |
| 3 | Stage contracts (`backend/app/contracts/`) | done |
| 4 | Docker Compose: app-db, target-db, Oracle, Redis, api, worker | done |
| 5 | Platform DB with Alembic (projects, source files, jobs) | done |
| 6 | Celery + Redis jobs, tracked in app-db | done |
| 7 | Sample legacy system with planted traps (`samples/oracle/`) | done |
| 8 | Ingestion → IR → conversion (Ora2Pg, SQLGlot, Gemini) | upload done; IR + conversion next |
| 9 | Validation: harness, comparator, test generation | next |
| 10 | LangGraph workflow, human checkpoints, repair loop | |
| 11 | React dashboard | |
| 12 | Integration, demo script, deck | |

## Quick start

Prerequisites: Docker Desktop (give it ≥ 6 GB RAM; Oracle needs about 2 GB),
Python 3.12, Git.

```bash
cp .env.example .env                 # then paste your GEMINI_API_KEY into .env
docker compose up -d --build         # first Oracle start takes a few minutes
docker compose ps                    # wait until oracle shows (healthy)
```

The `api` container runs `alembic upgrade head` on start, so the platform tables
exist as soon as it is up. API docs: <http://localhost:8000/docs>

Then, from your laptop:

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install oracledb "psycopg[binary]" redis

python scripts/check_env.py      # Oracle OUT-param call, both Postgres DBs, Redis, '' vs NULL demo
python scripts/seed_oracle.py    # load (or reset) the sample legacy system into Oracle
python scripts/smoke_api.py      # health -> project -> ZIP upload -> files -> Celery job
```

All three should end in PASS. Unit tests run inside the container:

```bash
docker compose run --rm api pytest -q
```

## Layout

```
backend/
  app/
    main.py              FastAPI app, CORS, /api/v1/health
    config.py            settings from env (.env via docker-compose)
    worker.py            Celery app
    api/                 REST routes + response schemas
    contracts/           Pydantic models + interfaces between pipeline stages
    database/            SQLAlchemy models, session
    ingestion/           safe extraction of uploaded SQL / ZIP
    orchestration/       Celery tasks, job tracking, (next) LangGraph graph
    services/            llm_service.py: the only code that calls Gemini
    migration/           (next) IR, Ora2Pg / SQLGlot / Gemini conversion
    validation/          (next) execution harness, comparator, test generation
    reporting/           (next) migration report
  alembic/               migrations (0001: projects, source_files, jobs)
  tests/
samples/oracle/          the "legacy" system + EXPECTED.md (golden answers)
scripts/                 check_env.py, seed_oracle.py, smoke_api.py
contracts/               exported JSON schemas
frontend/                (step 11) React + Vite
```

## Database changes (Alembic)

```bash
docker compose run --rm api alembic revision --autogenerate -m "add ir_objects"
# read the new file in backend/alembic/versions/ before committing
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic heads     # must print exactly one head
```

If two branches both added a revision, `alembic heads` shows two: run
`alembic merge heads -m "merge"` and commit the merge file.

## Working rules

1. Pull before you start, push before you stop. Small commits with clear messages
   (`feat(validation): oracle execution harness`).
2. Work on short-lived branches and merge through a PR, even between the two of you,
   so `main` always runs.
3. Never commit `.env` or API keys. Only synthetic sample code goes to Gemini.
4. Generated code only ever runs in `target-db`, never in `app-db`.
5. `samples/oracle/EXPECTED.md` is the answer key. If a real Oracle run disagrees
   with it, Oracle wins: fix the file.
