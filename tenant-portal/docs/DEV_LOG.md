# Development Log: Tenant Portal

This file records every step of the project: what we did, which commands we ran,
which files we created, why, and how to test it yourself.

---

## How we work (agreed 2026-09-30)

1. Work in small steps, never a full phase at once.
2. Before each step: say what will be done, which commands will run, and which
   files will be created or changed. Then wait for approval.
3. After each step: explain what was done in simple words, show how to test it,
   and update this log. Then stop.
4. Continue only after the reply "ok" or "continue".
5. Questions are answered before doing anything else.

---

## Status overview

| Phase | Topic | Status |
|---|---|---|
| 0 | Research (tools, library versions, library internals) | Done |
| 1 | Project setup (Docker, settings, health, docs) | Stack runs, `/health/` returns 200 (Step 3). `/api/docs/` not checked yet |
| 2 | Data model (Tenant, User, AuthEvent) | Migrations apply (Step 3). `createsuperuser` not tested yet |
| 3 | Email + password authentication | Code written, **not tested** |
| 4 | RBAC and tenant isolation | Code written, **not tested** |
| 5 | TOTP MFA for platform admins | Code written, **not tested** |
| 6 | Google sign-in (allauth headless) | Code written, **not tested** |
| 7 | Security hardening | Mostly written inside the settings, **not tested** |
| 8 | Tests and README | Not started |

---

## The big picture (for the presentation)

The platform is split into microservices. The **Tenant Portal** is the service
that knows *who* a user is. Nobody else reads its database.

1. A user logs in to the portal (email + password, or Google).
2. The portal gives back a **JWT access token**. A JWT is a small signed text
   that says "this is user X, from tenant Y, with role Z". It is valid 15 minutes.
3. The portal signs the token with its **private key** (RS256).
4. The other services (orchestrator, RAG, and so on) check the signature with
   the portal's **public key**, published at `/.well-known/jwks.json`. If the
   signature is valid, they trust `tenant_id` and `role` inside the token. They
   never need to call the portal or read its tables.
5. To stay logged in, the browser also holds a **refresh token** (valid 7 days)
   in a special cookie that JavaScript cannot read. Every 15 minutes the frontend
   asks `/api/auth/refresh/` for a new access token. Each refresh token works
   **once**: after use it goes on a blacklist and a new one is given.

---

## Step 0: Research (Phase 0)

**Goal:** check the machine, choose exact library versions, and read how each
library works inside before writing code, so the design matches the libraries
as they are today.

### Commands and why

| Command | Why |
|---|---|
| `docker --version`, `docker compose version`, `git --version`, `python --version` | Check which tools exist. Result: Docker works, **Python is not installed on Windows**, so everything (including tests) runs inside Docker. |
| `docker info` | Check that the Docker engine is running. |
| `docker run --rm python:3.12-slim pip index versions <package>` | Ask PyPI for the newest version of every library, inside a clean container. |
| `docker run -d --name probe python:3.12-slim sleep 3600` then `pip install ...` in it | Install all libraries together in a temporary container to check they work together, then read their source code. |
| `docker rm -f probe` | Delete the temporary container. |
| `openssl version` | Check that OpenSSL exists in Git Bash (needed to create the RSA keys). |

### What we found and what we decided

1. **Django version.** The newest Django is 6.1, but you asked for 5.x, so we
   use **Django 5.2.17** (5.2 is the long-term support version).
2. **Extra packages that are needed but were not in your list** (they are
   required by packages that were in your list):
   - `psycopg[binary]`: the driver that lets Python talk to PostgreSQL.
   - `gunicorn`: the production web server (you mentioned it).
   - `cryptography`: comes with `djangorestframework-simplejwt[crypto]`. It is
     required to sign tokens with RS256.
   - `Pillow`: comes with `qrcode[pil]`. It is required to draw the QR code as a PNG image.
   We did **not** add anything else (no pyotp, no freezegun, no Redis, no
   JSON-logging library).
3. **simplejwt cannot add a `kid` header** to tokens. `kid` = "key id"; it tells
   other services which public key to use. Decision: write a small class
   `KidTokenBackend` that adds it.
4. **django-otp** reads the TOTP secret only through one property called
   `bin_key`. Decision: make a "proxy" model that stores the secret
   **encrypted** and decrypts it in `bin_key`. All of django-otp's checking
   logic (time window, anti-replay) stays the same.
5. **django-otp's anti-replay** (a code cannot be used twice) is not safe if two
   requests arrive at the same moment. Decision: lock the database row while
   checking (`select_for_update`).
6. **allauth headless:** if our code raises an error during Google login,
   allauth sends the user back to the frontend with `?error=<code>`. Decision:
   put our Google rules (verified email, no auto-linking) in one place, the
   adapter.
7. **allauth** already trusts the hosts listed in `CSRF_TRUSTED_ORIGINS` for
   redirects, so we only need to list the frontend there.
8. **django-axes** allows a custom "lockout response". Decision: make it return
   the same `"Invalid email or password."` error as every other failure.
9. **Swagger UI** (the API docs page) needs scripts from a CDN and one inline
   style. Decision: keep the strict security policy everywhere, and relax it
   only on `/api/docs/`.

---

## Step 1: Project setup (Phase 1)

**Goal:** a project that starts with one command (`docker compose up`), with
all secrets outside the code.

### Folder structure

```
tenant-portal/
├── docker-compose.yml       # starts db + portal + mailpit
├── Dockerfile               # how the portal image is built
├── .env.example             # list of all settings, fake values (committed)
├── .env                     # real values for your machine (NOT committed)
├── .gitignore               # keeps .env and keys/ out of git
├── .gitattributes           # keeps .sh files with Linux line endings
├── .dockerignore            # keeps .env and keys/ out of the image
├── requirements.txt         # libraries for production (exact versions)
├── requirements-dev.txt     # + pytest, for development only
├── keys/                    # RSA keys (NOT committed)
├── scripts/
│   ├── generate_keys.sh     # creates the RSA key pair
│   └── entrypoint.sh        # runs before the server starts in the container
├── docs/DEV_LOG.md          # this file
└── src/                     # the Django project
    ├── manage.py
    ├── pytest.ini
    ├── config/              # settings, urls, logging, health, JWKS
    ├── accounts/            # Tenant, User, login, MFA, Google, permissions
    └── audit/               # AuthEvent (security log)
```

### Files and what they do

- **`Dockerfile`**
  - Starts from `python:3.12-slim` and installs the libraries.
  - Creates a user `app` (uid 1000), so the server does **not** run as root.
    If someone breaks in, they have fewer rights.
  - Default command: `gunicorn` (production server).
  - Build option `INSTALL_DEV=true` also installs pytest. docker compose uses
    it; a production image does not have pytest.
- **`scripts/entrypoint.sh`**
  - Runs before the server. If `RUN_MIGRATIONS=true`, it creates or updates the
    database tables (`migrate`) and the cache table (`createcachetable`).
  - In Kubernetes, migrations should run once in a Job, not in every pod,
    so this is off by default.
- **`docker-compose.yml`**: three services:
  - `db`: image `pgvector/pgvector:pg16` (PostgreSQL 16 with pgvector for RAG
    later). A named volume `pgdata` keeps the data when containers stop. A
    health check with `pg_isready` says when the database is ready.
  - `portal`: our app. It **waits until `db` is healthy**, runs `runserver`
    (auto-reload), and mounts `./src` so code changes apply without a rebuild.
    `./keys` is mounted read-only at `/keys`.
  - `mailpit`: catches every email the app sends. SMTP on 1025, web page on
    http://localhost:8025.
  - All ports are bound to `127.0.0.1`, so other computers on your network
    cannot reach them.
- **`.env.example` / `.env`**: all configuration (secret key, database
  password, CORS origins, key paths, MFA key, Google credentials, email).
  `.env` was created with random secrets using `openssl rand`.
- **`scripts/generate_keys.sh`**: creates `keys/jwt_private.pem` (signs tokens,
  must stay secret) and `keys/jwt_public.pem` (checks tokens, can be shared).
  RSA 2048 bits.
- **`src/config/settings/`**: settings split in four files:
  - `base.py`: everything shared. Every secret is read from the environment.
  - `dev.py`: local development (DEBUG on, cookies allowed without HTTPS).
  - `prod.py`: production. DEBUG is forced off, HTTPS redirect, HSTS. It
    **refuses to start** if `ALLOWED_HOSTS` is empty or `*`, or if CORS allows
    `http://`.
  - `test.py`: tests. It creates **temporary** RSA keys and secrets, so tests
    never touch your real keys.
- **`src/config/views.py`**:
  - `GET /health/`: runs `SELECT 1` on the database. 200 if OK, 503 if not.
    Kubernetes uses it as the **readiness** probe.
  - `GET /health/live/`: always 200 while the process runs (**liveness**
    probe). If the database is down, Kubernetes should not restart the app.
  - `GET /.well-known/jwks.json`: the public key in JWK format.
- **`src/config/logging.py`**: every log line is one JSON object with a
  timestamp, level, message and `correlation_id`. Emails are masked
  (`j***@gmail.com`) and anything that looks like a JWT is replaced with
  `[REDACTED_JWT]`.
- **`src/config/middleware.py`**: reads the `X-Correlation-ID` header or
  creates one, adds it to every log line and to the response. You can then
  follow one request through all the microservices. It also writes one log
  line per request (method, path, status, duration).
- **`src/config/urls.py`**: all URLs. API docs at `/api/docs/`.

### Commands run in this step

```sh
sh scripts/generate_keys.sh       # created keys/jwt_private.pem and keys/jwt_public.pem
# .env created from .env.example with random values:
#   DJANGO_SECRET_KEY  <- openssl rand -base64 48
#   POSTGRES_PASSWORD  <- openssl rand -hex 16
#   MFA_ENCRYPTION_KEY <- openssl rand -base64 32
docker compose build              # build the portal image (worked)
```

---

## Step 2: Data model (Phase 2)

**Goal:** create the tables. The custom User model must exist **before the
first migration**, because Django cannot switch user models later.
`AUTH_USER_MODEL = "accounts.User"` is set in `base.py`.

### Models (file `src/accounts/models.py` and `src/audit/models.py`)

- **Tenant**: a client company (a clinic, for example).
  - Fields: `id` (UUID), `name`, `slug` (unique short name for URLs), `is_active`, `created_at`, `updated_at`.
  - `is_active=False` means **suspended**.
- **User**: a person who logs in to the portal. (The clinic's customers who talk to the voice agent are **not** users.)
  - Fields: `id` (UUID), `tenant`, `email` (unique, used to log in), `first_name`, `last_name`, `role`, `is_active`, `is_email_verified`, `is_staff`, `failed_login_attempts`, `locked_until`, `last_login`, `created_at`, `updated_at`.
  - `role` is `platform_admin` (runs the platform) or `tenant_admin` (manages one clinic).
  - Emails are always saved in lowercase, so `Ali@X.com` and `ali@x.com` are the same account.
  - `createsuperuser` creates a `platform_admin` with a verified email and no tenant.
- **Two database rules (check constraints):**
  1. A `platform_admin` can **never** have a tenant.
  2. A `tenant_admin` **must** have a tenant. The only exception: a new Google
     user who has not finished onboarding. Those users have no password
     (an "unusable password"), so the rule is "no tenant is only allowed
     without a password". Email/password users always get their tenant at
     registration, in the same transaction.
  The same rules are also checked in Python (`User.clean()`), so the error is
  clear before the database refuses it.
- **AuthEvent** (security log): who did what, from which IP, with which browser.
  - Event types: register, email_verified, login_success, login_failed, account_locked, logout, token_refresh, password_reset_requested, password_reset_done, mfa_enrolled, mfa_success, mfa_failed, google_login.
  - It **never** stores passwords, tokens, MFA codes, or the email someone typed.
- **MFAChallenge**: a pending second login step for admins with MFA (explained in Phase 5).
- **EncryptedTOTPDevice** and **BackupCodeDevice**: "proxy" models. They use
  django-otp's tables but change how the secret is stored (explained in Phase 5).

### Other files

- `src/audit/services.py`: `log_auth_event(...)` writes an AuthEvent and a log line.
- `src/audit/utils.py`: `get_client_ip()` finds the real client IP. It trusts
  the `X-Forwarded-For` header only for the number of proxies set in
  `NUM_PROXIES`, so a client cannot fake its IP. Throttling, axes and the audit
  log all use the same function.

### Commands run in this step

```sh
docker compose run --rm --no-deps -e RUN_MIGRATIONS=false portal \
    python manage.py makemigrations accounts audit
```
- First try: **failed** with a circular import (see Problem 1 below).
- Second try, after the fix: **worked**. It created
  `accounts/migrations/0001_initial.py` and `audit/migrations/0001_initial.py`.
  (The warning `failed to resolve host 'db'` is normal: `--no-deps` does not
  start the database, and `makemigrations` does not need it.)

---

## Code written ahead (Phases 3 to 7): written, NOT tested yet

This code was written before we switched to small steps. Each part will be
tested in its own step, and this log will be updated then.

### Phase 3: Email + password login

| File | What it does |
|---|---|
| `accounts/services/registration.py` | Register: creates the Tenant + User in **one transaction** (both or nothing). If the email already exists, it creates nothing and sends an "you already have an account" email instead, so the response is the same (no one can find out which emails are registered). Also email verification, resend, and onboarding. |
| `accounts/tokens.py` | Signed links for emails. The verification link includes `is_email_verified` in its signature, so it stops working once used (single use). Valid 24 h. The password reset link is valid 1 h. |
| `accounts/services/authentication.py` | Login: if the email is unknown, it still computes a password hash, so the response time is the same (timing attack protection). After 5 wrong passwords, the account is locked 15 min. Same error for every failure: `"Invalid email or password."`. Also refresh (rotation + blacklist) and logout. |
| `accounts/services/passwords.py` | Password reset: generic response; on confirm, sets the new password, unlocks the account, and **logs out all devices** (blacklists all refresh tokens). |
| `accounts/services/emails.py` + `templates/accounts/email/*.txt` | The three emails. If sending fails, the error is logged, not shown to the client. |
| `accounts/jwt.py` | RS256 tokens with `kid`, claims `tenant_id`, `role`, `email_verified`; the JWKS; the refresh cookie (`HttpOnly`, `SameSite=Strict`, `Path=/api/auth/`). |
| `accounts/views/auth.py`, `views/common.py`, `serializers.py`, `urls.py` | The API endpoints and input checks. |

### Phase 4: Roles and tenant isolation

| File | What it does |
|---|---|
| `accounts/permissions.py` | `IsPlatformAdmin`, `IsTenantAdmin`, `HasTenant`, plus a default gate: a user who has not finished onboarding (or an admin without MFA, when MFA is required) is blocked everywhere except `me`, onboarding, MFA setup and logout. |
| `accounts/authentication.py` | Checks on **every request** that the user is active and the tenant is not suspended. A suspended tenant loses access immediately, not after 15 minutes. |
| `accounts/mixins.py` | `TenantScopedQuerysetMixin` for future views (documents, sessions...): always filters by the logged-in user's tenant. The tenant is **never** read from the request. |
| `accounts/views/tenants.py` | `GET /api/admin/tenants/`, `POST /api/admin/tenants/{id}/suspend/`, `GET /api/tenant/me/`. |

### Phase 5: TOTP MFA

| File | What it does |
|---|---|
| `accounts/services/mfa.py` | Setup (QR code, issuer `AIVoice`), confirm (returns **10 backup codes once**), and the login challenge: `mfa_token` valid 5 min, max 5 attempts, TOTP or backup code. |
| `accounts/crypto.py` | The TOTP secret is **encrypted** (AES-SIV) with a key made from `MFA_ENCRYPTION_KEY`. Backup codes are stored only as a keyed hash. A stolen database alone reveals neither. |
| `accounts/views/mfa.py` | The MFA endpoints. |

### Phase 6: Google sign-in

| File | What it does |
|---|---|
| `accounts/adapters.py` | Rules: only Google, only with `email_verified: true`, users identified by Google `sub` (not email), **no automatic linking** to an existing email/password account (error `email_already_registered`). New Google users: `tenant_admin`, no tenant, no password. |
| `accounts/services/google.py`, `views/google.py` | After Google login, allauth creates a short session. `POST /api/auth/google/exchange/` turns it into **our own** JWTs and destroys the session. `POST /api/auth/onboarding/` creates the tenant and returns new tokens with `tenant_id`. |

### Phase 7: Security (inside `settings/base.py` and `prod.py`)
- HSTS, HTTPS redirect (prod), secure cookies, `X_FRAME_OPTIONS="DENY"`, nosniff, referrer policy.
- Content Security Policy: `default-src 'none'` (the API only returns JSON).
- CORS: only origins from `.env`, with credentials allowed (for the refresh cookie).
- Rate limits: register 5/h, login 10/min, password reset 5/h, MFA 10/min, resend 3/h.
- django-axes: second lock, 10 failures per IP + email, 1 hour.
- JSON logs with correlation id, no sensitive data.

---

## Problems we hit and how we solved them

### Problem 1: circular import (solved)
- **Symptom:** `makemigrations` crashed with
  `ImportError: cannot import name 'APIView' from partially initialized module 'rest_framework.views'`.
- **Cause:** DRF loads our file `accounts/permissions.py` while its own file
  `rest_framework/views.py` is still loading. Our file imported `APIView` from
  that half-loaded file, so each file was waiting for the other.
- **Fix:** `APIView` and `Request` are only needed for type hints, so they are
  now imported inside `if TYPE_CHECKING:` (only read by code editors, never at
  runtime).

### Problem 2: circular migration dependency (solved in Step 3)
- **Symptom:** `docker compose up -d` started `db` and `mailpit`, but `portal`
  crashed during `migrate` with
  `CircularDependencyError: accounts.0001_initial, otp_totp.0001_initial, ...`.
- **Cause:** `accounts/0001_initial` creates the User table **and** the two MFA
  proxy models. The proxies need django-otp's tables first. But django-otp's
  tables need the User table first. Each migration waits for the other.
- **Planned fix:** split into two migrations:
  `0001_initial` (Tenant, User, MFAChallenge) and `0002_mfa_devices` (the two
  proxies). Order: our User, then django-otp, then our proxies.

---

## Small fixes made along the way
- `accounts/tokens.py`: an invalid id in an email link was caught with a
  catch-all `except Exception`. It now catches only the expected errors.
- `accounts/tokens.py`: the reset token class was renamed
  `PortalPasswordResetTokenGenerator`, so it does not hide Django's class of
  the same name.
- `accounts/serializers.py`: two almost identical password-check functions were
  merged into one (`validate_password_policy`).
- `base.py`: our custom header settings were renamed `AUTH_CUSTOM_HEADER*`, so
  they cannot be confused with Django's own `CSRF_HEADER_NAME`.

---

## Step 3: Fix the migration loop and start the app (2026-09-30)

**Goal:** the portal container starts, creates all tables, and `/health/` returns 200.

### The problem (reminder)
`accounts/0001_initial` created the User table **and** the two MFA proxy models.
The proxies need django-otp's tables. django-otp's tables need our User table.
So Django found a loop: `accounts 0001 -> otp_totp -> accounts 0001`.

### The fix: split into two migrations
| Migration | Creates | Depends on |
|---|---|---|
| `accounts/0001_initial.py` | Tenant, User, MFAChallenge, the 2 constraints | Django `auth` only |
| `accounts/0002_mfa_devices.py` | EncryptedTOTPDevice, BackupCodeDevice (proxies) | our `0001` + django-otp (`otp_totp`, `otp_static`) |

Now the order is a straight line: **our User → django-otp's tables → our proxies**.

### Commands and why
```sh
# 1. Back up models.py so it can be restored exactly
cp src/accounts/models.py <scratchpad>/models.py.bak

# 2. Temporarily cut the two proxy classes from models.py, delete the old migration
head -n 165 src/accounts/models.py > models.tmp && mv models.tmp src/accounts/models.py
rm src/accounts/migrations/0001_initial.py

# 3. audit's migration points at accounts 0001, which was just deleted, so
#    Django refused to run. Moved it aside for a moment (content unchanged).
mv src/audit/migrations/0001_initial.py <scratchpad>/

# 4. Create the new 0001 (--skip-checks: the URL checks import the MFA code,
#    which needs the classes we cut for a moment)
docker compose run --rm --no-deps -e RUN_MIGRATIONS=false portal \
    python manage.py makemigrations accounts --skip-checks

# 5. Put the audit migration back, restore models.py (checked identical with cmp)
mv <scratchpad>/audit_0001_initial.py src/audit/migrations/0001_initial.py
cp <scratchpad>/models.py.bak src/accounts/models.py

# 6. Create 0002 for the proxies
docker compose run --rm --no-deps -e RUN_MIGRATIONS=false portal \
    python manage.py makemigrations accounts --name mfa_devices

# 7. Start the app (entrypoint.sh runs migrate + createcachetable)
docker compose up -d portal
```

### Files
- `src/accounts/migrations/0001_initial.py`: recreated (without the proxies).
- `src/accounts/migrations/0002_mfa_devices.py`: new.
- `src/accounts/models.py`: **unchanged** at the end.
- `src/audit/migrations/0001_initial.py`: **unchanged** (only moved and put back).

### Result
- All 3 containers run: `db`, `mailpit`, `portal`.
- All migrations applied (`[X]` for accounts 0001 + 0002, audit 0001, otp_totp, token_blacklist...).
- `GET /health/` → `200 {"status": "ok", "database": "ok"}`.
- The response already shows the security headers from Phase 7:
  `Content-Security-Policy: default-src 'none' ...`, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
  and `X-Correlation-ID` (from our middleware).

### How to test it yourself
```sh
cd "D:\AI Voice Reception Platform\tenant-portal"

# 1. All three services should be "running"
docker compose ps

# 2. Health check: expect HTTP/1.1 200 and {"status": "ok", "database": "ok"}
curl -i http://127.0.0.1:8000/health/
#    (or open http://127.0.0.1:8000/health/ in your browser)

# 3. Liveness: expect {"status": "ok"}
curl http://127.0.0.1:8000/health/live/

# 4. See which migrations are applied: every line should have [X]
docker compose exec portal python manage.py showmigrations accounts audit otp_totp

# 5. Bonus: send your own correlation id, it comes back in the response
curl -i -H "X-Correlation-ID: my-test-123" http://127.0.0.1:8000/health/

# 6. Bonus: see what happens when the database is down (expect 503)
docker compose stop db
curl -i http://127.0.0.1:8000/health/
docker compose start db
```

---

## Step 4: Run the project locally without Docker (Option B) (2026-09-30)

**Goal:** run `python manage.py runserver` from the `tenant-portal` conda
environment (Python 3.12), while PostgreSQL and Mailpit still run in Docker.

### Done by you before this step
- Conda environment `tenant-portal` with Python 3.12 (`D:\miniconda\envs\tenant-portal`).
  Base Miniconda is Python 3.14 and was not touched.
- `python -m pip install -r requirements-dev.txt` inside that environment.
- `.env.local` created (copy of `.env.example`), in `tenant-portal\` (not committed).
- `dev.py` loads `.env.local` **before** `from .base import *`:
  ```python
  PROJECT_ROOT = Path(__file__).resolve().parents[3]   # = tenant-portal\
  environ.Env.read_env(PROJECT_ROOT / ".env.local", overwrite=False)
  ```
  `overwrite=False`: a variable already set in the environment (for example by
  docker compose) wins over the file.
- `docker-compose.yml` publishes PostgreSQL on `127.0.0.1:5432` (only your own PC can reach it).
- `docker compose stop portal` to free port 8000.

### Problem 3: `Set the DATABASE_URL environment variable`
- **Symptom:** after fixing `DJANGO_SECRET_KEY`, Django stopped on `DATABASE_URL`.
- **Was `.env.local` loaded?** Yes. Proof: the first error (`DJANGO_SECRET_KEY`)
  disappeared, and that value only exists in the `.env` files.
- **Cause:** `.env.local` was copied from `.env.example`, where the line is a
  comment: `# DATABASE_URL=...`. Lines starting with `#` are ignored.
- **Also wrong for a local run** (values made for inside Docker):
  - `JWT_PRIVATE_KEY_PATH=/keys/...`: `/keys` only exists inside the container.
  - `EMAIL_HOST=mailpit`: the name `mailpit` only exists inside Docker's network.

### Problem 4: the database connection hangs with `localhost`
- **Symptom:** `manage.py check` passed, but `showmigrations` hung for more than 2 minutes.
- **Test:** connecting directly with a 5-second limit:
  `127.0.0.1` → OK in 1.2 s; `localhost` → OK only after 5.1 s.
- **Cause:** on Windows, `localhost` is tried first as IPv6 (`::1`). Docker
  publishes the port only on IPv4 (`127.0.0.1`). The IPv6 try waits, and
  Django sets no connection time limit, so it hangs.
- **Fix:** use `127.0.0.1` instead of `localhost`.

### Changes made in `.env.local` (4 lines, nothing else)
| Line | Before | After |
|---|---|---|
| 18 | `# DATABASE_URL=...` (comment) | `DATABASE_URL=postgres://portal:<POSTGRES_PASSWORD from .env>@127.0.0.1:5432/tenant_portal` |
| 27 | `JWT_PRIVATE_KEY_PATH=/keys/jwt_private.pem` | `JWT_PRIVATE_KEY_PATH=D:/AI Voice Reception Platform/tenant-portal/keys/jwt_private.pem` |
| 28 | `JWT_PUBLIC_KEY_PATH=/keys/jwt_public.pem` | `JWT_PUBLIC_KEY_PATH=D:/AI Voice Reception Platform/tenant-portal/keys/jwt_public.pem` |
| 45 | `EMAIL_HOST=mailpit` | `EMAIL_HOST=127.0.0.1` |

- The password was copied from `.env` automatically and never printed. It must
  be the **same** as in `.env`, because the database was created with it.
- A backup of the old `.env.local` is in Claude's scratchpad folder (temporary).
- **Not changed:** `base.py`, `dev.py`, `docker-compose.yml`, `.env`.

### Result
- `python manage.py check` → `System check identified no issues`.
- `python manage.py showmigrations accounts audit` → all `[X]`, in about 2 seconds.
- `python manage.py runserver` → `GET /health/` = `200 {"status": "ok", "database": "ok"}`.
- The server prints JSON log lines with a `correlation_id` (from our middleware).

### How to test it yourself
```powershell
# 1. Database + Mailpit in Docker (not the portal container)
cd "D:\AI Voice Reception Platform\tenant-portal"
docker compose up -d db mailpit
docker compose stop portal          # only if it is running (frees port 8000)

# 2. Run Django from the conda environment
conda activate tenant-portal
cd src
python manage.py check              # expect: no issues
python manage.py showmigrations     # expect: [X] everywhere
python manage.py runserver

# 3. In the browser or another terminal
curl http://127.0.0.1:8000/health/  # expect: {"status": "ok", "database": "ok"}
```
Mailpit (emails): http://127.0.0.1:8025

**Remember:** use `127.0.0.1`, not `localhost`, in `.env.local` on Windows.

---

## Step 5: Swagger "Authorize" button (2026-10-04)

### Problem
In Swagger (`/api/docs/`), `GET /api/auth/me/` always returned **401**, and
there was no 🔒 **Authorize** button for entering a token.

### Why
Swagger only shows Authorize when the OpenAPI schema says how users
authenticate. drf-spectacular describes simplejwt's `JWTAuthentication`
automatically, but **not subclasses**. We use our own subclass,
`PortalJWTAuthentication`, so the schema said nothing about authentication.
Swagger never sent the `Authorization: Bearer ...` header.

### Changes
- **New** `src/accounts/schema.py`: `PortalJWTScheme`, a drf-spectacular
  extension that says "`PortalJWTAuthentication` = HTTP Bearer, JWT".
- **Changed** `src/accounts/apps.py`: `ready()` imports `accounts.schema` so
  the extension is registered at startup.

### Result
- `python manage.py check` → no issues.
- `python manage.py spectacular` → the schema now has
  `securitySchemes: jwtAuth (http, bearer, JWT)`.
- `/api/auth/me/` has `security: jwtAuth`. Public endpoints like
  `/api/auth/login/` keep `security: {}` (no token needed).

### How to test it yourself
1. `python manage.py runserver`, then open http://127.0.0.1:8000/api/docs/.
   A 🔒 **Authorize** button appears at the top right.
2. `POST /api/auth/login/` → **Try it out** → your email and password →
   copy the `access` value from the response.
3. Click **Authorize** and paste **only the token** (Swagger adds `Bearer `).
   Click **Authorize**, then **Close**.
4. `GET /api/auth/me/` → **Execute** → expect **200** with your user.

The access token lasts 15 minutes. After that you get 401 again, so log in
again and re-authorize. Platform admins with MFA get an `mfa_token` instead of
an access token. They must finish the MFA step first.

---

## Next step (waiting for approval)

Same as before (Phase 1 and Phase 2 exit checks), now run locally:
1. Check that `/api/docs/` (Swagger) and `/.well-known/jwks.json` work.
2. Run `python manage.py createsuperuser` and check that the new user is a
   `platform_admin`, with `is_email_verified=True` and no tenant.

No files change, except this log (and a code fix only if a check fails).

---

## Fix: `POST /api/auth/mfa/verify/` returned 500 (2026-10-11)

### Problem
Every MFA verify call returned **500**. PostgreSQL log:
`FOR UPDATE cannot be applied to the nullable side of an outer join`.

`verify_mfa_challenge` used `select_for_update()` together with
`select_related("user", "user__tenant")`. `tenant` is nullable, so Django
uses a `LEFT OUTER JOIN`, and a bare `select_for_update()` tries to lock
**every** joined table. PostgreSQL refuses to lock the nullable side of a
LEFT JOIN. Platform admins (the only users with MFA) have no tenant, so the
query always failed.

### Change
- **Changed** `src/accounts/services/mfa.py`:
  `select_for_update()` → `select_for_update(of=("self",))`.
  SQL becomes `FOR UPDATE OF "accounts_mfachallenge"`: only the challenge
  row is locked. That is the row we update (attempts, consumed_at), so the
  attempt limit and the "one code accepted once" rule still hold.

### Result
- The query was run against the local PostgreSQL (port 5433): no error,
  SQL ends with `FOR UPDATE OF "accounts_mfachallenge"`.

### How to test it yourself
1. `POST /api/auth/login/` with the platform admin → copy `mfa_token`.
2. `POST /api/auth/mfa/verify/` with `{"mfa_token": "...", "code": "123456"}`
   (fresh code from the authenticator app) → expect **200** with `access`.
3. Wrong code → **401** `mfa_code_invalid` (no more 500).

---

## Test setup: Google sign-in (2026-10-11)

### Why
Google sign-in starts with a browser redirect and ends on a **frontend**
callback page, so it cannot be tested from Swagger alone. There is no
frontend yet, so two small test pages act as the frontend on
`http://localhost:3000` (kept outside the repo).

- `index.html`: gets the CSRF cookie (`GET /_allauth/browser/v1/config`), then
  sends a form POST to `/_allauth/browser/v1/auth/provider/redirect`
  (`provider=google`, `process=login`, `callback_url=.../callback.html`).
- `callback.html`: shows `?error=<code>` if allauth refused the login,
  otherwise calls `POST /api/auth/google/exchange/`, then offers
  `GET /api/auth/me/` and `POST /api/auth/onboarding/`.

### Prerequisites
- Google Cloud Console OAuth client (Web), redirect URI
  `http://localhost:8000/accounts/google/login/callback/`; consent screen in
  *Testing* with your Gmail added as a test user.
- Backend on **`localhost:8000`** (not `127.0.0.1`): cookies are shared
  between `localhost:3000` and `localhost:8000`, not with `127.0.0.1`.

### Test plan
| # | Scenario | Expected |
|---|---|---|
| 1 | New Gmail signs in | exchange 200; `me` = tenant_admin, no tenant |
| 2 | Onboarding | 201 with `tenant_id`; again → 409 |
| 3 | Same Google account again | 200, tenant already set |
| 4 | Gmail already used by an email/password account | `?error=email_already_registered` |
| 5 | Exchange twice | second → 401 (single use) |
| 6 | Exchange without `X-Requested-With` | 403 |

---

## Fix: Google exchange always returned 401 (2026-10-11)

### Problem
The Google login itself worked (user and `SocialAccount` created), but
`POST /api/auth/google/exchange/` always answered
**401 "No Google login to exchange."**

`exchange_google_session` used `request.user`. The view is a
`PublicAPIView` (`authentication_classes = []`), so DRF sets the user to
`AnonymousUser` and also writes it onto the underlying Django request
(`rest_framework/request.py`, `Request.user` setter:
`self._request.user = value`). The user that Django had loaded from the
allauth session was lost before our code ran.

### Change
- **Changed** `src/accounts/services/google.py`: `user = request.user` →
  `user = get_user(request)` (`django.contrib.auth.get_user`, reads the user
  from the session with Django's normal checks). The other rules are
  unchanged: Google login less than 5 minutes old, session destroyed after
  the exchange.

### Result (Django test client, inside a rolled-back transaction)
- Simulated fresh Google session → exchange **200** with `access_token`.
- Second exchange → **401** (single use).
- No `X-Requested-With` header → **403**.

### How to test it yourself
Open `http://localhost:3000/index.html`, sign in with Google again (the
failed attempt destroyed the old session) → callback shows
`Exchange -> HTTP 200`. Then **GET /api/auth/me/** and **Onboarding**.

Note: the test page `callback.html` read `access` instead of
`access_token`; fixed in the copy in `D:\google-test\google-test`.

---

## CI step 2.1: first automated tests (2026-10-23)

### Goal
The auth service had no tests. Before writing the GitLab CI, add 4 simple
tests that cover the ISO/IEC 25010 quality characteristics and run them for
real (no simulated results).

### Changes
- **Changed** `requirements-dev.txt`: added `pytest-cov==7.1.0`, `ruff==0.17.2`,
  `bandit==1.9.4` (pinned).
- **New** `src/.coveragerc`: coverage counts application code only
  (tests, migrations and settings are excluded, so the % is honest).
- **New** `src/accounts/tests/test_unit_passwords_tokens.py` — test (a), unit:
  Argon2 hash + verify, valid / expired / tampered JWT.
- **New** `src/accounts/tests/test_api_auth_flow.py` — test (b), API:
  register → verify email (link read from the test mailbox) → login → `/api/auth/me/`
  = 200; wrong password = 401; no token = 401.
- **New** `src/accounts/tests/test_roles.py` — test (c), roles: a tenant_admin gets
  200 on `/api/tenant/me/` but 403 on `/api/admin/tenants/`.
- Test (d), SAST, is Bandit: no test file, it scans `src/`.
- **Changed** `.gitignore`: ignore local `reports/` and `.coverage`.

The database is PostgreSQL (the same as production). SQLite is not used anywhere.

### Real results (run inside the dev Docker image, Python 3.12.14, Postgres 16)
- pytest: **8 passed** in 8.96 s. Coverage of application code: **69 %**
  (1213 statements, 374 missed).
- Bandit (`--severity-level medium`): **no medium/high issues**, exit code 0.
  8 low-severity notes, all false positives (strings such as `'token_refresh'`
  or `'Bearer'` next to a variable name containing "token"/"password").
- Ruff (classic error rules E4, E7, E9, F): 2 findings, both E402 in
  `config/settings/dev.py` (intentional: `.env.local` must be loaded before
  the base settings are imported). To be handled in step 2.2.

### How to test it yourself
```bash
cd tenant-portal
docker compose build portal
docker compose up -d db
docker compose run --rm -e RUN_MIGRATIONS=false portal pytest -v --cov --cov-report=term
docker compose run --rm --no-deps --entrypoint "" portal bandit -r . -x ./accounts/tests,./audit/tests,./accounts/migrations,./audit/migrations --severity-level medium
```

---

## CI step 2.2: GitLab CI pipeline (2026-10-23)

### Goal
Run lint, the 4 tests and the Bandit scan automatically in GitLab, only when
the tenant-portal changes, with reports visible in GitLab and kept as proof.

### Changes
- **New** `.gitlab-ci.yml` (repository root, parent pipeline): job `tenant-portal`
  uses `trigger: include: tenant-portal/.gitlab-ci.yml` + `strategy: depend`, with
  `rules: changes: [tenant-portal/**/*, .gitlab-ci.yml]`. A `workflow` block avoids
  running a branch pipeline and an MR pipeline for the same push.
- **New** `tenant-portal/.gitlab-ci.yml` (child pipeline), stages lint → test → security:
  - `ruff` — lint, JUnit report `reports/ruff-junit.xml`.
  - `pytest` — tests (a), (b), (c) against a throwaway `pgvector/pgvector:pg16`
    service (alias `db`). JUnit `reports/report.xml`, Cobertura `reports/coverage.xml`,
    coverage % read from the `TOTAL` line of the log.
  - `bandit` — test (d). Full JSON report, JUnit XML of medium/high findings,
    and the job fails on any medium/high finding.
  - All reports are saved as artifacts for 30 days, even when a job fails.
  - Tool versions are read from `requirements-dev.txt` (one place to change them).
- **New** `src/ruff.toml`: ruff's classic error rules (E4, E7, E9, F).
- **Changed** `src/config/settings/dev.py`: `# noqa: E402` on the two imports that
  must come after `read_env()` (intentional order), plus a comment explaining why.

### CI variable to create in GitLab
`TEST_DB_PASSWORD` (Settings → CI/CD → Variables): letters and digits, 16+ chars,
**Masked**, **not Protected** (otherwise it is empty on the `imen-dev` branch).
If it is missing, the pytest job stops with a clear error message.

### Local check (real run)
Each job's script was run exactly as written in the YAML, in a clean
`python:3.12-slim` container, with Postgres as a separate container named `db`
(a stand-in for a GitLab runner; the real proof is the GitLab run):
- ruff: `All checks passed!`, exit 0
- pytest: `8 passed in 10.10s`, `TOTAL 1213 374 69%`, coverage regex reads `69%`, exit 0
- bandit: `No issues identified.`, exit 0
- Missing `TEST_DB_PASSWORD` → job stops with the error message, exit 1.

Note: with no findings, `bandit-junit.xml` contains 0 test cases, so Bandit adds
nothing to the Tests tab when the code is clean. The proof is the job log and
`bandit-report.json`.

---

## CI step 2.3: testing documentation (2026-10-23)

- **New** `TESTING.md`: traceability table (requirement | test | tool |
  ISO 25010 characteristic | how to see the result), where to find the
  results in GitLab, the required CI variable, how to run the tests locally,
  and the latest real local results.
- **Changed** `.gitlab-ci.yml` (child): comment on the `lint` stage now says
  "code quality check" (coverage is measured in the `pytest` job, not in lint).
