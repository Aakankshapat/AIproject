# FocusExam — AI-proctored test portal (Python + Streamlit only)

Separate **Admin** and **Client** portals on top of the FocusLab focus-detection engine.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```
Open the app → pick a portal. Direct links: `?portal=client` and `?portal=admin`.
Demo logins: `admin / admin123` · `client / client123` (set `ADMIN_PASSWORD` before first run to change the seed admin password).
The camera needs `localhost` or HTTPS.

## The 3-strike rule
Every proctoring violation is a strike: no face (4s+), multiple faces, focus under 35% for 8s+, camera off.
Each strike shows a toast + live strike dots; the 2nd shows "final warning"; **at 3 strikes the test ends automatically**
and is graded on the answers saved so far. (Limit and thresholds are configurable per test / in Settings.)

## Pre-loaded tests (10 questions each)
1. Python Fundamentals · 2. Logical & Quantitative Aptitude (25% negative marking) ·
3. Web & Networking Essentials · 4. Cybersecurity Awareness — each allows 2 attempts, 3 strikes.

## Admin portal
Dashboard · **Live Monitor** (watch sessions, end a test remotely) · Tests (category, difficulty, negative marking,
attempts, strikes, CSV import) · Clients · Results & Proctoring (focus timeline, violations, answers, integrity score, export) ·
**Analytics** (leaderboard, question difficulty, violation types) · Settings (rules, ML calibration, extra admins)

## Client portal
Dashboard · Take Test (camera check → rules → timed test, auto-save/resume) · My Results (score, integrity, rank, top performers) · Profile

## Security / speed
PBKDF2 passwords · role-checked logins · 5 failed logins → 60s lock · SQLite WAL + indexes + batched writes ·
frames downscaled and analysed every 4th frame · focus stored once per second · 1s fragment keeps questions from re-rendering.
No images are ever saved. Existing databases migrate automatically.
