# Diagnostics: how to tell what's wrong

How the CAIC voucher system reports problems, and how to read each signal. Written for the
admin or IT person looking after the server. Each signal says **where to look**, **what it
means** and **what to do**.

Fault tests that measure how fast these signals lead to a fix are in [FAULT_TESTS.md](FAULT_TESTS.md).

**Contents**

1. [The system at a glance](#1-the-system-at-a-glance)
2. [Status lights (LEDs)](#2-status-lights-leds)
3. [Error codes](#3-error-codes)
4. [Logs](#4-logs)
5. [Watchdogs](#5-watchdogs)
6. [Where to start: symptom → first check](#6-where-to-start-symptom--first-check)
7. [Command reference](#7-command-reference)
8. [Step-by-step repairs: start, restart or wait](#8-step-by-step-repairs-start-restart-or-wait)
9. [Backups and restoring](#9-backups-and-restoring)

The same content, with more explanation, is in the printable guide [CAIC_Diagnostics_Guide.pdf](CAIC_Diagnostics_Guide.pdf).

---

## 1. The system at a glance

Everything runs in Docker on one server. Each box is a container (`docker compose -p backend ps`):

```
Browser ──► caic-web (nginx, port 80) ──► caic-api (backend, 4 processes) ──► postgres-db (database)
                     │                                                          ▲
                     └──► caic-assistant (/api/chat) ──► caic-ollama (AI model) │
                                                                                │
caic-backup (nightly encrypted backups) ────────────────────────────────────────┘
caic-autoheal (watchdog: restarts containers that fail their health check)
```

A problem in one box shows up in a predictable way in the others. That's what the signals
below are for.

---

## 2. Status lights (LEDs)

The app has no hardware LEDs. These coloured dots on screen play the same role:

| Light | Where | Green | Yellow | Red / grey |
|---|---|---|---|---|
| **Live dot** | Top bar, next to the user menu (everyone) | **Live**: connected; other people's changes appear by themselves | — | **Reconnecting…**: the live connection dropped. Screens stop updating by themselves. The app retries on its own |
| **System status** | **Settings → Status** (admins) | Every part is working | Working, but something needs a look | **Red**: a part isn't working. **Grey**: turned off on purpose |
| **One light per part** | Settings → Status table | | | |
| — Database | | Answers (time in ms) | `W-DB-02` slow (over 500 ms) | `E-DB-01` can't be reached |
| — Backups | | Last backup under 26 h ago, and the backup service alive | `W-BK-04` backup service stopped (no heartbeat for 3 min), `W-BK-02` none in 26 h, or `W-BK-03` *Back up now* not started after 5 min | `E-BK-01` the last backup failed |
| — Automatic clean-up | | Ran in the last 2 h | `W-PU-01` hasn't run in 2 h | grey: `PURGE_ENABLED=false` |
| — Server errors (24 h) | | None | `W-ER-01` some, with the latest error ID | — |
| — Live updates | | Watching (number of open screens) | — | `E-LV-01` the watcher stopped |
| — Assistant (AI model) | | Model ready | `W-AI-02` model not downloaded | `E-AI-01` model server unreachable; grey: turned off |
| **Backup light** | Settings → System → Backups | Last backup within a day | Over a day old | The last backup failed (reason shown) |
| **Presence dots** | Settings → Manage Users, Active Status | Active | Idle (open, unused 7+ min) | Offline |
| **Container health** | `docker ps` (STATUS column), Docker Desktop | `(healthy)` | `(health: starting)` | `(unhealthy)`: autoheal restarts it |

**Settings → Status** refreshes every 30 seconds. **Check now** refreshes straight away.
**Copy report** copies a text summary (time, app version, every light with its code) to paste
into a message to whoever fixes it.

**Reading Settings → Status when the server itself is down:** the page shows
`E-NET-01` (no connection) or `E-NET-02` (the server isn't answering). The status check needs
the backend, so in that case go to [section 7](#7-command-reference).

---

## 3. Error codes

### 3.1 Codes from the status check

Every code in Settings → Status. `E-` codes are failures (red), `W-` codes are warnings (yellow).
The same list is in `Backend/health.py` (`CODES`).

| Code | Meaning | What to do |
|---|---|---|
| `E-DB-01` | The database can't be reached | `docker compose -p backend ps`: is `postgres-db` running? Start it with `docker start postgres-db`. Read `docker logs postgres-db` |
| `W-DB-02` | The database answers slowly (over 500 ms) | A big import or export may be running. Check the server's CPU, memory and disk in Task Manager |
| `E-AI-01` | The assistant's model server can't be reached | `docker start caic-ollama`; read `docker logs caic-ollama` |
| `W-AI-02` | The assistant's model isn't downloaded | `docker compose -p backend exec ollama ollama pull qwen3:4b-instruct-2507-q4_K_M` |
| `E-BK-01` | The last backup failed | Read the reason (status table, or Activity Log → record type *Backups*). Usual causes: wrong or missing `BACKUP_PASSPHRASE`, backups folder missing or full. `docker logs caic-backup` |
| `W-BK-02` | No backup in the last 26 hours | Is `caic-backup` running? `docker start caic-backup`. Then **Back up now** |
| `W-BK-03` | *Back up now* hasn't started after 5 minutes | The backup container isn't running: `docker start caic-backup` |
| `W-BK-04` | The backup service stopped: it reports "alive" every minute (heartbeat) and hasn't for 3 minutes (60 while a backup is running) | `docker start caic-backup`; read `docker logs caic-backup` |
| `W-PU-01` | The automatic clean-up hasn't run in 2 hours | `docker restart caic-api`; read `docker logs caic-api` (lines starting `[purge]`) |
| `E-LV-01` | Live updates stopped | `docker restart caic-api`. Lines starting `[live]` in its log give the reason |
| `W-ER-01` | Server errors in the last 24 hours | Activity Log, action **Server error**. Each has an error ID to look up in `docker logs caic-api` (see 3.3) |

### 3.2 Codes people see on screen

| Message shows | Meaning | What to do |
|---|---|---|
| `E-NET-01` "Can't reach the server" | The browser got no answer at all | Is the server on? Is this computer online (and on Tailscale, if used)? If only one person has it, it's their connection |
| `E-NET-02` "The server isn't answering (HTTP 502/504)" | nginx (`caic-web`) is up, but the backend (`caic-api`) isn't answering | `docker compose -p backend ps`. If `caic-api` is restarting, wait a minute; if it's stopped, `docker start caic-api` |
| `E-DB-01` "…its database isn't (HTTP 503)" | The backend is up, the database isn't | As `E-DB-01` above |
| `E-` + 8 characters, e.g. `E-1A2B3C4D` | An unexpected error in the backend (a bug, or something it depends on is down) | See 3.3 |
| HTTP `401` / "Your session ended" | Signed out: inactivity, signed in elsewhere, or the account was deactivated | Sign in again. Not a fault |
| HTTP `403` | Not allowed for this account (e.g. staff on an admin action), or a feature turned off in Settings | Not a fault |
| HTTP `409` | Someone else changed the same record first, or a PO number is taken | Reopen the record and try again |
| HTTP `422` | Invalid input, e.g. CV/PO with letters | Fix the input; the message says what's wrong |
| HTTP `429` | Too many tries: wrong passwords, sign-ups or reset requests | Wait (the message says how long) or use Forgot password |

### 3.3 Error IDs (unexpected server errors)

When the backend hits an error it didn't expect, it:

1. Gives the person a message with a short **error ID**, e.g. *"Something went wrong on the
   server (error E-1A2B3C4D)…"*.
2. Writes the full details (where, what, Python traceback) to the backend log under that ID.
3. Adds a **Server error** row to the Activity Log with the ID, the page/route and the error type.
4. Counts it in Settings → Status (`W-ER-01`, with the latest ID).

**To find the cause from an error ID:**

```powershell
docker logs caic-api 2>&1 | Select-String -Context 0,30 "E-1A2B3C4D"
```

If the database was down at the time, step 3 can't be written: the log line
`couldn't write it to the Activity Log` says so, and the database is the cause.

---

## 4. Logs

Every container writes its log times in **Philippine time (UTC+8)**: the backend's lines start with the date and time (`2026-11-18 00:57:03 INFO: ...`), and the web server, database, AI model and watchdog logs use the same time zone (`TZ` in `Backend/docker-compose.yml`). Only the extra time that `docker logs -t` adds in front is UTC (it ends in `Z`), because Docker itself always records UTC; leave out `-t`.

| Log | How to open it | What's in it | Kept |
|---|---|---|---|
| **Activity Log** | User Panel → Activity Log (admins). Filter by action, user, record type, text | Every action (who, what, when, old → new values), sign-ins and failed sign-ins, imports and exports, **Server error** rows with their error ID, backup results, settings changes | Forever, unless *Delete routine Activity Log rows after* is set (Settings → System); changes to records are always kept |
| **Backend log** | `docker logs caic-api` (add `-f` to follow, `--since 1h` to limit) | Every request (method, path, status code), error tracebacks (`[error E-…]`), clean-up runs (`[purge]`), live-update problems (`[live]`) | Until the container is recreated (next deploy) |
| **Assistant log** | `docker logs caic-assistant` | Same as the backend log, for `/chat` | Until the next deploy |
| **Web server log** | `docker logs caic-web` | Every page and API request through nginx, with status code; `502` lines mean the backend didn't answer | Until the next deploy |
| **Database log** | `docker logs postgres-db` | Start-up, shutdown, connection errors, slow or failed queries | Until the next deploy |
| **Backup log** | `docker logs caic-backup` | Each backup: saved (size) or FAILED (reason); settings changes; pruned files | Until the next deploy. Results are also in the Activity Log |
| **Watchdog log** | `docker logs caic-autoheal` | Each automatic restart of an unhealthy container | Until the next deploy |
| **Deploy log** | `.deploy\deploy.log` in the project folder | Every deploy: branch, commit, version, success or the reason it failed | Kept |

**Reading the backend log:** each request line looks like
`INFO: 172.18.0.5:40122 - "GET /projects HTTP/1.1" 200 OK`. The number after `HTTP/1.1` is the
status code (see 3.2). `[error E-…]` blocks are unexpected errors; the last line of the
traceback names the cause.

**Tip:** container logs disappear when a container is recreated by a deploy. Copy what you
need first: `docker logs caic-api > api-log.txt 2>&1`.

---

## 5. Watchdogs

Parts of the system that check other parts and fix or report problems by themselves:

| Watchdog | Watches | Checks every | When something's wrong | Where you see it |
|---|---|---|---|---|
| **Docker restart policy** (`restart: unless-stopped`) | Every container | Continuously | A container that crashes or exits is started again straight away. One stopped by hand (`docker stop`) stays stopped | `docker ps` STATUS "Up 5 seconds"; `docker inspect -f "{{.RestartCount}}" caic-api` |
| **Docker health checks** | `caic-api` and `caic-assistant` (`/health`), `caic-web` (nginx), `caic-ollama` (`ollama list`), `postgres-db` (`pg_isready`) | 10–60 s | After 3 failures in a row the container is marked **unhealthy** | `docker ps`: `(healthy)` / `(unhealthy)`; Docker Desktop |
| **autoheal** (`caic-autoheal`) | Containers labelled `autoheal=true`: api, assistant, web, ollama | 30 s | Restarts any that are **unhealthy** (e.g. a backend that's running but frozen) | `docker logs caic-autoheal` |
| **Database start order** | The backend and backup containers wait until `postgres-db` is healthy | At start | They don't start against a database that isn't ready | `docker compose -p backend ps` |
| **Backup retry** | The nightly backup | Every minute | After a failure it tries again an hour later; a missed 2 AM backup runs as soon as the server is on | Backup light, `E-BK-01` / `W-BK-02` |
| **Backup heartbeat** | The backup service | Every minute | Writes "alive" to the database; if it stops, Status shows `W-BK-04` within about 3 minutes | Status table: "service alive … ago" |
| **Clean-up heartbeat** | The hourly clean-up of archived items and old log rows | Each run | Records each run; if it stops, Settings → Status shows `W-PU-01` | Status table: "Last run …" |
| **Live-update watcher** | New Activity Log rows, pushed to open screens | 1.5 s | Retries after a database hiccup; if it stops, `E-LV-01` | Live dot; Status table |
| **Browser reconnect** | Each screen's live connection | Continuously | Reconnects by itself; the dot shows **Reconnecting…** meanwhile | Live dot |
| **Session watchdog** | Each signed-in screen | 1 s | Warns 2 minutes before the inactivity sign-out; signs out at the end | "You'll be signed out in…" bar |
| **Update check** | The deployed version | 1 min | Offers **Refresh** when a new version is deployed | "A new version of the app is available" bar |

**Note:** while the database is down, the backend's health check fails too, so autoheal
restarts `caic-api` every couple of minutes. That's harmless, and stops once the database is
back. Fix the database, not the backend.

---

## 6. Where to start: symptom → first check

| Symptom | First check | Likely cause | Code |
|---|---|---|---|
| Nobody can open the app | Is the server on? `docker compose -p backend ps` | Server off or asleep; Docker Desktop not running; `caic-web` stopped | `E-NET-01` |
| The app opens but every action fails | Settings → Status, or `docker compose -p backend ps` | `caic-api` stopped or restarting | `E-NET-02` |
| Every action fails with "database isn't" | `docker ps` for `postgres-db` | Database stopped, or disk full | `E-DB-01` |
| One action fails with an error ID | Activity Log → Server error; look up the ID in `docker logs caic-api` | A bug, or bad data in that record | `E-xxxxxxxx` |
| Other people's changes don't appear | Live dot | Live connection dropped (network), or the watcher stopped | `E-LV-01` |
| The assistant doesn't answer | Settings → Status → Assistant | `caic-ollama` stopped, model missing, or turned off | `E-AI-01`, `W-AI-02` |
| Backup light yellow or red | Settings → Status → Backups; `docker logs caic-backup` | Backup container stopped, wrong passphrase, folder missing | `E-BK-01`, `W-BK-02`, `W-BK-03`, `W-BK-04` |
| Archived items aren't being deleted | Settings → Status → Automatic clean-up | Clean-up stopped, or `PURGE_ENABLED=false` | `W-PU-01` |
| One person keeps getting signed out | Activity Log for that user | Signed in on another device, inactivity limit, deactivated account | (HTTP 401) |
| One person can't sign in | Activity Log: *Failed sign-in* | Wrong password lockout (429), account waiting for approval or deactivated | (HTTP 429/403) |

---

## 7. Command reference

Run in PowerShell on the server, from any folder:

```powershell
docker compose -p backend ps                    # every container: running? healthy?
curl.exe http://localhost/api/health            # {"status":"ok"} or 503 {"status":"down","code":"E-DB-01"}
docker logs caic-api --since 30m                # backend log, last 30 minutes
docker logs caic-api 2>&1 | Select-String "E-1A2B3C4D" -Context 0,30   # find an error ID
docker logs caic-autoheal                       # automatic restarts
docker inspect -f "{{.State.Health.Status}} {{.RestartCount}}" caic-api   # health and restart count
docker restart caic-api                         # restart the backend (a few seconds)
docker start postgres-db                        # start a stopped container
Get-Content .deploy\deploy.log -Tail 20         # last deploys (project folder)
```

---

## 8. Step-by-step repairs: start, restart or wait

Find the situation that matches and do the steps **in order**; stop when the *Done when* line is true.

### 8.1 The one rule

Look first: `docker compose -p backend ps`, then act on what the box's line says.

| You see for the box | Meaning | Do |
|---|---|---|
| **Line missing** (or Exited) | Stopped. Docker won't start it by itself (stopped by hand, or crashed and gave up) | `docker start <box>`, wait 15–30 s |
| **Restarting** / **(health: starting)** | Already coming back | **Wait** 1–2 min; don't interrupt it |
| **(unhealthy)** | Running but failing its health check; autoheal restarts it within ~2 min | **Wait** 2 min; still unhealthy → read its log, then `docker restart <box>` |
| **Up (healthy)** but the app still fails | Stuck, or holding an old broken connection (e.g. after the database came back) | `docker restart <box>` |
| **Several missing** | Docker restarted, or many boxes stopped | Start all (below) |

```powershell
# Starts every stopped box; running ones are left alone, so it's always safe
docker start postgres-db caic-api caic-assistant caic-web caic-ollama caic-backup caic-autoheal
```

> **Never** run `docker compose down -v`, `docker volume rm` or `docker system prune --volumes`: they **delete the database**.

### 8.2 Nobody can open the app (E-NET-01 for everyone)
1. Server on, awake and signed in to Windows? (Docker Desktop only runs while someone is signed in.)
2. Docker Desktop running (whale icon)? If not, start it and wait 1–2 min.
3. `docker compose -p backend ps`: caic-web missing → `docker start caic-web`; several missing → start all.
4. `http://localhost` works on the server but not elsewhere → network: `tailscale status`, `tailscale up`.
5. Only one person affected → their computer/connection.

*Done when:* the app opens on the server and another computer; `curl.exe http://localhost/api/health` says ok.

### 8.3 App opens, every action fails (E-NET-02)
1. `docker compose -p backend ps` → caic-api.
2. Missing/Exited → `docker start caic-api`, wait 15–30 s.
3. Restarting / (health: starting) → wait 1–2 min.
4. (unhealthy) > 2 min, or healthy but still failing → `docker logs caic-api --tail 40`, then `docker restart caic-api`.
5. Log mentions the database (*connection refused*) → go to 8.4.
6. Keeps failing at start-up → save the log (`docker logs caic-api > api-log.txt 2>&1`) and redeploy (8.9).

*Done when:* health ok, sign-in and opening a project work, Status green.

### 8.4 Database (E-DB-01)
1. `docker compose -p backend ps` → postgres-db.
2. Missing/Exited → `docker start postgres-db`; wait for **(healthy)** (10–20 s).
3. Won't start → `docker logs postgres-db --tail 30`; *No space left on device* = disk full: free space, start again.
4. Database healthy but actions still fail → `docker restart caic-api caic-assistant`.

Autoheal restarting caic-api while the database is down is expected; fix the database first.

*Done when:* health ok, a project opens, Status → Database green.

### 8.5 Backup light (W-BK-02/03/04, E-BK-01)
1. **W-BK-04 / W-BK-03** → `docker start caic-backup`; within a minute: *service alive … ago*.
2. **W-BK-02** → start it as above, then Settings → System → **Back up now**.
3. **E-BK-01** → read the reason (or `docker logs caic-backup --tail 20`): database down (8.4); disk/folder full or missing (free space); BACKUP_PASSPHRASE missing (put the **same** one back in `Backend\.env`, run `.\deploy.ps1`).
4. Click **Back up now**, wait a minute.

*Done when:* Activity Log shows a new *Backup saved* row; Status → Backups green.

### 8.6 Assistant (E-AI-01, W-AI-02)
1. Grey = turned off (Settings → System → Chat assistant).
2. E-AI-01 → `docker start caic-ollama` (and `docker start caic-assistant` if missing).
3. W-AI-02 → pull the model (section 3.1).
4. First answer after a start is slow; that's normal.

### 8.7 One action fails with an error ID
Look up the ID (3.3). A box problem → its section above. Same record fails every time → a bug: send the ID, steps and log block to the developer; restarting won't fix it.

### 8.8 After a power cut / Windows restart
1. Sign in to Windows; wait for Docker Desktop (1–2 min), then 2 more minutes.
2. `docker compose -p backend ps`: a box stopped **by hand** before the restart stays stopped → `docker start <box>` or start all.
3. Daily check. A missed nightly backup runs by itself.

### 8.9 Nothing worked: redeploy
1. Save logs (`docker logs <box> > <box>-log.txt 2>&1`); a redeploy clears them.
2. Project folder: `.\deploy.ps1` (rebuilds and starts every box; the data is kept in the database volume).
3. Daily check.

---

## 9. Backups and restoring

A backup is a full, encrypted copy of the database: projects, POs and PO files, entries, users, settings, logo, Activity Log. Not included: the code (GitHub) and `Backend\.env` (keep your own copy; without **BACKUP_PASSPHRASE** no backup can be opened).

| When | Folder | Kept (default) |
|---|---|---|
| Daily at the backup hour (Settings → System), or as soon as the server is on if missed | `backups\daily` | 7 |
| Weekly copy | `backups\weekly` | 4 |
| Monthly copy | `backups\monthly` | 6 |
| Back up now, `.\backup.ps1`, before a restore | `backups\manual` | all |

**Back up now:** Settings → System → **Back up now**, or `.\backup.ps1 -Label before-import`.

**Restore** (on the server, project folder; not in the app on purpose). For a single deleted project, use the **Archive** in the app instead.

```powershell
.\restore.ps1 -List                                                     # 1. list backups
.\restore.ps1 -File daily\caic_db_2026-11-18_020000.dump.gpg            # 2. test: into caic_db_restore, real data untouched
.\restore.ps1 -File daily\caic_db_2026-11-18_020000.dump.gpg -ReplaceLive   # 3. replace the real data (type YES)
```

Step 3 backs up the current data to `backups\manual` first, keeps the old database as `caic_db_replaced_<date>`, and restarts the backend. Everything goes back to the moment of that backup; people may need to sign in again. Test a restore (steps 1–2) once a month.
