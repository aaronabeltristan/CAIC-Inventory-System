# Fault tests: measuring MTTD and MTTR

Planned faults, caused on purpose, to measure how quickly the team **notices** a problem
(MTTD, mean time to detect) and **fixes** it (MTTR, mean time to repair) using the diagnostics
in [DIAGNOSTICS.md](DIAGNOSTICS.md).

> **The results tables below are empty on purpose.** Fill them in from your own runs
> (`tools\fault-timer.ps1` writes every run to `docs\fault-results.csv`). Don't copy example
> numbers into a report.

---

## 1. Definitions

| Time | Meaning |
|---|---|
| **T0** | The moment the fault is caused |
| **T-detect** | The moment a person notices something is wrong (a symptom, a red light, an error code) |
| **T-repaired** | The moment it's fixed **and checked**: Settings → Status green (or the expected light), and a normal action works (e.g. open a project) |
| **TTD** (time to detect) | T-detect − T0 |
| **TTR** (time to repair) | T-repaired − T-detect: diagnosing plus fixing |
| **Downtime** | T-repaired − T0 = TTD + TTR |
| **MTTD / MTTR** | The **average** TTD / TTR over the runs of one fault (at least 3 runs each) |
| **Monitor detection** | When `/api/health` first failed (recorded by the script). Shows how fast the system itself notices, compared with a person |

---

## 2. How to run a test

**Before:** do it outside working hours, tell users the app will be interrupted, and check that
Settings → Status is all green (or note what isn't). Have a backup (**Back up now**).

**Roles (3 people):**

- **Injector:** runs the script, which causes the fault and starts the clock. Doesn't tell the others which fault.
- **Detector:** uses the app normally: opens projects, adds an entry, uses the assistant, looks at Settings → Status from time to time (every few minutes, as an admin would). Says "detected" out loud when they notice a problem, and how.
- **Repairer:** diagnoses and fixes, using only the diagnostics (status lights, error codes, logs, the commands in DIAGNOSTICS.md section 7). Says "repaired" once the checks pass.

**Steps:**

1. On the server, in PowerShell, in the project folder:
   ```powershell
   .\tools\fault-timer.ps1 -Fault backend -Run 1 -Detector "<name>" -Repairer "<name>"
   ```
   Faults: `backend`, `database`, `backup`, `assistant`, `crash`.
2. The script causes the fault and prints **T0**. It checks `http://localhost/api/health` every second and prints each change.
3. When the detector says "detected", the injector presses **D** and types how it was noticed (e.g. `E-NET-02 on screen`).
4. When the repairer says "repaired", the injector presses **R**.
5. The script asks for the tools used and the repair steps, then saves the run to `docs\fault-results.csv` and prints TTD, TTR and downtime.
6. Repeat 3 times per fault, changing who detects and who repairs. Vary the moment of the fault, so the detector can't guess it.
7. Copy the averages into section 5.

**Without the script:** use a stopwatch or `Get-Date` in PowerShell, write down T0, T-detect and
T-repaired in the run sheet (section 6), and cause the fault with the command shown for it.

---

## 3. The faults

### F1: Backend stopped

| | |
|---|---|
| **Cause** | `docker stop caic-api` (script: `-Fault backend`) |
| **Simulates** | The backend crashed and didn't come back, or someone stopped it |
| **Expected symptoms** | Every action fails with **E-NET-02** "The server isn't answering (HTTP 502)". The Live dot shows **Reconnecting…**. Settings → Status can't load (E-NET-02). `docker ps` doesn't list `caic-api`. `/api/health` fails |
| **Diagnose** | `docker compose -p backend ps` (caic-api missing or *Exited*); `docker logs caic-web --tail 20` (502 lines) |
| **Repair** | `docker start caic-api`; wait about 15 s; check Settings → Status and open a project |
| **Tools** | Browser (error code, Live dot), PowerShell, `docker compose ps`, `docker logs`, Settings → Status |
| **Watchdog** | A container stopped by hand isn't restarted (by design), so a person has to notice it |

### F2: Database stopped

| | |
|---|---|
| **Cause** | `docker stop postgres-db` (script: `-Fault database`) |
| **Simulates** | The database crashed, or the disk filled up |
| **Expected symptoms** | Actions fail with an error ID (**E-xxxxxxxx**) or **E-DB-01**. Settings → Status: Database red **E-DB-01**. `curl.exe http://localhost/api/health` answers 503 `{"status":"down","code":"E-DB-01"}`. After about 1.5 minutes `caic-api` turns **unhealthy** and autoheal restarts it (this doesn't fix the database) |
| **Diagnose** | Settings → Status (E-DB-01); `docker compose -p backend ps` (postgres-db *Exited*); `docker logs caic-api` (*connection refused* to `db:5432`); `docker logs caic-autoheal` |
| **Repair** | `docker start postgres-db`; wait until `docker ps` shows it *(healthy)*; check Settings → Status and open a project. If the backend still fails, `docker restart caic-api caic-assistant` |
| **Tools** | Settings → Status, `curl.exe .../api/health`, `docker compose ps`, `docker logs`, Activity Log (the error IDs) |

### F3: Backup service stopped

| | |
|---|---|
| **Cause** | `docker stop caic-backup`, then click **Back up now** in Settings → System (script: `-Fault backup`; it reminds you to click) |
| **Simulates** | Backups silently stopped: the most dangerous kind of fault, because the app keeps working |
| **Expected symptoms** | Nothing visible to staff. *Back up now* shows "Backup requested…" and never finishes. After 5 minutes Settings → Status: Backups yellow **W-BK-03**. Without *Back up now*, the nightly backup is missed and **W-BK-02** shows after 26 hours |
| **Diagnose** | Settings → Status (W-BK-03); `docker compose -p backend ps` (caic-backup *Exited*) |
| **Repair** | `docker start caic-backup`; within a minute it makes the requested backup; check Settings → System → Backups (green, new time) and Status (green) |
| **Tools** | Settings → Status and System → Backups, `docker compose ps`, `docker logs caic-backup`, Activity Log (*Backup saved*) |
| **Note** | TTD here is limited by design (the 5-minute and 26-hour thresholds). Record it anyway: it shows why item 3 proposes alerts |

### F4: AI model server stopped

| | |
|---|---|
| **Cause** | `docker stop caic-ollama` (script: `-Fault assistant`) |
| **Simulates** | The assistant's model server crashed or ran out of memory |
| **Expected symptoms** | The rest of the app works. The assistant answers with an error. Settings → Status: Assistant red **E-AI-01** |
| **Diagnose** | Settings → Status (E-AI-01); `docker compose -p backend ps`; `docker logs caic-assistant` |
| **Repair** | `docker start caic-ollama`; ask the assistant a question (the first answer takes longer while the model loads); check Status |
| **Tools** | Assistant chat, Settings → Status, `docker compose ps`, `docker logs` |

### F5: Backend crash with automatic recovery (watchdog test)

| | |
|---|---|
| **Cause** | Ends the backend's main process from inside the container (script: `-Fault crash`) |
| **Simulates** | The backend process crashes on its own |
| **Expected symptoms** | A few seconds of **E-NET-02** and **Reconnecting…**, then everything works again by itself |
| **Repair** | None: Docker's restart policy starts it again. The script ends by itself when `/api/health` is ok again and records the monitor's detection and recovery times |
| **Check afterwards** | `docker inspect -f "{{.RestartCount}}" caic-api` went up by one; `docker logs caic-api` shows the shutdown and start |
| **Shows** | The difference a watchdog makes: compare its downtime with F1 (the same fault, without automatic repair) |

At least three faults are required. F1, F2 and F3 are the core set; F4 and F5 are extra.

---

## 4. Tools used (summary)

| Tool | Used for |
|---|---|
| Browser (the app) | Symptoms and on-screen error codes (E-NET-01/02, E-DB-01, error IDs), Live dot |
| **Settings → Status** | Status light and code for each part; **Copy report** for the hand-over |
| **Activity Log** | *Server error* rows with error IDs; *Backup saved / FAILED* |
| PowerShell + Docker CLI | `docker compose -p backend ps`, `docker logs`, `docker inspect`, `docker start/restart` |
| `curl.exe http://localhost/api/health` | Quick health check without signing in |
| Docker Desktop | Container list with health (`healthy` / `unhealthy`) |
| `tools\fault-timer.ps1` | Causing the fault, timing T0 / detect / repair, the monitor's own detection, saving results |

---

## 5. Results (fill in)

Averages of your runs, from `docs\fault-results.csv`. Times in seconds.

| Fault | Runs | MTTD | MTTR | Mean downtime | Monitor detection (mean) | How it was usually detected | Tools used |
|---|---|---|---|---|---|---|---|
| F1 Backend stopped | | | | | | | |
| F2 Database stopped | | | | | | | |
| F3 Backup service stopped | | | | | | | |
| F4 AI model server stopped | | | | | | | |
| F5 Backend crash (auto) | | | | | | | |

**Observations** (fill in): which signal led to detection fastest; where the repairer lost time
(finding the cause, or applying the fix); which faults weren't noticed by staff at all.

---

## 6. Run sheet (one per run, if not using the script)

| Field | Value |
|---|---|
| Fault (F1–F5) | |
| Run number | |
| Date | |
| Injector / Detector / Repairer | |
| T0 (fault caused) | |
| T-detect (noticed) | |
| How it was noticed (symptom, light, code) | |
| T-repaired (fixed and checked) | |
| TTD = T-detect − T0 | |
| TTR = T-repaired − T-detect | |
| Downtime = T-repaired − T0 | |
| Diagnosis steps | |
| Repair steps | |
| Tools used | |
| Notes (what slowed things down) | |

---

## 7. After the tests

- Check everything is back: `docker compose -p backend ps` (all *Up*, *healthy*) and Settings → Status green.
- The Activity Log may show *Server error* rows from the tests (not while the database itself was down: those are only in `docker logs caic-api`). That's expected; note their IDs in the run sheets.
- `docs\fault-results.csv` holds every run (one row each). Open it in a text editor, or in Excel to average the columns.
