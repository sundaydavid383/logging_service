LOCAL_STACK_RUN_COMPATIBLE.md
# 🟢 Local Stack Run Guide (Docker-Compatible Configuration)

This guide starts PostgreSQL + Redis (Memurai) locally on Windows while preserving full compatibility with:

- docker-compose.yml
- .env configuration
- fdq_commons/config.py

No code changes required.

All services are mapped exactly as in production container setup.

---

# 🧠 Architecture Mapping (IMPORTANT)

| Service | Local Equivalent | Port |
|--------|----------------|------|
| PostgreSQL (Docker) | PostgreSQL Windows Service | 5432 |
| Redis (Docker) | Memurai Redis | 6379 |

Both must match `.env` values exactly.

---

# 🟡 1. PostgreSQL (Docker-Compatible Local Mode)

## ▶️ Start PostgreSQL (Windows)

Ensure PostgreSQL service is running OR use CLI:

```bash id="u2x9lm"
psql -U postgres

OR direct path (if needed):

"C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres
🧪 Validate DB (must match docker DB name)

Your .env expects:

POSTGRES_DB=fdq_db
POSTGRES_USER=fdq_user
POSTGRES_PASSWORD=fdq_password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
Test inside psql:
SELECT datname FROM pg_database;

If fdq_db does not exist:

CREATE DATABASE fdq_db;
🧪 Test connection
SELECT 1;

Expected:

1
🟢 2. Redis (Memurai - Docker Compatible)
▶️ Start Redis CLI
memurai-cli

Expected:

127.0.0.1:6379>
🧪 Test Redis (must match docker redis service)
ping

Expected:

PONG
🧪 Test key storage
set test_key "fdq_local"
get test_key

Expected:

"fdq_local"
🟢 3. Environment Compatibility Check

Your system uses this .env (DO NOT CHANGE):

POSTGRES_HOST=localhost
POSTGRES_PORT=5432

REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
🧠 Why this works with config.py

Your settings.py already maps:

PostgreSQL
postgres_host = localhost
postgres_port = 5432
Redis
redis_host = localhost
redis_port = 6379
DSN generated automatically:
postgresql+psycopg2://fdq_user:fdq_password@localhost:5432/fdq_db
Redis URL:
redis://localhost:6379/0
🟢 4. Startup Order (VERY IMPORTANT)

Always start in this order:

1. PostgreSQL
Windows service OR CLI
2. Redis (Memurai)
memurai-cli
3. Backend services
uvicorn app.main:app --reload
🧪 5. Final System Verification
PostgreSQL check
psql -U postgres

Then:

SELECT 1;
Redis check
memurai-cli
ping

Expected:

PONG
🟢 6. Compatibility Guarantee

If both services return:

PostgreSQL → SELECT 1 works
Redis → PONG

Then:

✔ config.py works unchanged
✔ .env works unchanged
✔ docker-compose values preserved
✔ microservices behave identically to Docker environment