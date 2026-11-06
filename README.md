# Scout Hub

Self-hosted web app that **records VEX / VEXU match streams, splits them into
per-match clips, tags each clip with the teams and final score, and lets you
browse the library sorted by team.**

- Capture from a **live YouTube/Twitch stream**, an **already-archived YouTube/Twitch
  video** (a past broadcast — paste the normal watch URL; optionally give a
  `HH:MM:SS-HH:MM:SS` section to grab just part of a long stream), or a raw HLS
  (`.m3u8`). `vexworlds.tv` is Cloudflare-protected and simulcasts elsewhere, so
  paste its underlying YouTube/Twitch link or an `.m3u8` from the page.
- Records the **whole video to disk**, then a processing job finds each match's
  start/end and cuts a padded clip + thumbnail. It detects matches from the
  broadcast **match timer** when there's a real countdown, and otherwise from the
  overlay's **match-number label** (works with static Tournament Manager overlays,
  common on VEXU / regional streams).
- Team numbers and scores come from the **official VEX Events API**
  ([events.vex.com](https://events.vex.com), which replaced RobotEvents in 2026) when you
  link an event — just paste its events.vex.com link. This is the reliable path for **VEXU**, whose team codes
  (`BLRS1`, `ARC`, …) are hard to OCR. Without an event link, best-effort
  **on-screen OCR** fills in what it can and you correct it.
- **Match Library**: filter by team number, event, or round; sort by team, match
  number, schedule time, or score; "group by team" view with a W-L tally.

> Recording third-party streams may be against the platform's Terms of Service.
> You are responsible for how you use this tool.

## Architecture

| Piece | Stack |
|---|---|
| API + pipeline | Python 3.12, FastAPI, SQLModel/SQLite, APScheduler-style in-process worker |
| Capture / clipping | `yt-dlp`, `ffmpeg` |
| Match detection / OCR | OpenCV, Tesseract (`eng`) |
| Match data | VEX Events API v2 (`https://events.vex.com/api/v2`) |
| Frontend | React + TypeScript + Vite, TanStack Query |

```
recording (full stream)  ──►  detect match windows (timer OCR)
                                     │
              linked event? ──yes──► VEX Events schedule + align ──► teams & scores from API
                       │
                       └──no / leftover windows ──► overlay OCR ──► teams & scores (best effort)
                                     │
                                     ▼
                        ffmpeg per-match clip + thumbnail  ──►  Match Library
```

## Run with Docker (recommended)

1. Edit `docker-compose.yml` and change the **left side** of the `- ./media:/media`
   volume to wherever clips should be archived (a NAS mount, a big disk…). It
   appears at `/media` in the container and the in-app folder picker browses it.
2. Build & start:
   ```bash
   docker compose up -d --build
   ```
3. Open `http://<host>:8000` and click **Sign in**. The first time, there are no
   accounts yet, so it offers to **create the first admin account** — do this
   right away (or pre-seed one with `SCOUTHUB_ADMIN_USERNAME` /
   `SCOUTHUB_ADMIN_PASSWORD` in `docker-compose.yml`).
4. **VEX Events API key** (free, from your events.vex.com account). Either:
   - put `VEX_EVENTS_API_KEY=your-key` in a `.env` file next to
     `docker-compose.yml` (in **Dockge**: the stack's *.env* box), then redeploy —
     the compose file passes it to the app as `SCOUTHUB_VEXEVENTS_TOKEN`, and it
     **always wins** over a key typed in the web UI; or
   - paste it in **Admin → Settings & events → VEX Events API**.

   Then hit *Test connection* there; it says which key (environment or Settings
   page) it used. A key saved under the old RobotEvents setting is still used if
   nothing else is set.
5. **Admin → Recordings → Add recording**: paste the stream link (a live stream *or* an
   archived YouTube/Twitch video) and, optionally, the event's **events.vex.com
   link** (or its code, e.g. `RE-V5RC-26-4058`). The event is looked up and
   imported automatically. *Start recording* — stop a live one when the stream
   ends; a VOD stops itself.
   - Clips go to `/media/<Event name> (<code>)/`, or `/media/recordings/` with no
     event. To pick another folder, record only part of a long past stream
     (**Section**, `HH:MM:SS-HH:MM:SS`), or change live-from-start, open
     **More options**.
   - Events can also be imported ahead of time in **Admin → Settings & events → Events**.
6. Processing runs automatically. Watch it under *Recent processing jobs*, then
   open **Match Library**.

### Accounts & access

| Who | Can do |
|---|---|
| Visitors **without an account** | Browse and search the **Match Library** (by team, event, round), open a match, watch its clip. Nothing else — every write endpoint and the admin pages return `401`. |
| Any **signed-in account** | Everything in **Admin**: add/stop/re-process/delete recordings, import events, change settings, and edit/re-clip/delete matches. |
| **Admin** accounts | Additionally add and remove accounts, reset passwords, and grant admin (**Admin → Accounts**). The last admin can't be removed or demoted. |

Sign-ins are an HttpOnly, `SameSite=Lax` session cookie (30 days by default,
`SCOUTHUB_SESSION_DAYS`). If you serve Scout Hub over HTTPS, set
`SCOUTHUB_COOKIE_SECURE=true`. Passwords are stored as PBKDF2-SHA256 hashes.
There is no login rate limiting, so don't expose the app directly to the
internet without a reverse proxy in front of it.

### Processing time & when to trim

Detection OCRs sampled frames (spawning `tesseract`), so it runs at very roughly
**1.5-2.5x the video's length**. A 2-3 hour event processed overnight is fine; a
10-hour stream is not — use the **Section** field to target the matches you care
about. A coarse pass (every 5 s) locates matches, then a fine pass (1 fps)
re-scans only around each one.

Files land as:
```
/media/<your folder>/
  full/     <the complete recording>
  matches/  Qualification-12__1234A-5678B_vs_9999Z-4242C.mp4  (+ .jpg thumb)
```

## Local development

Prereqs on the host: Python 3.12+, Node 20+, `ffmpeg`, `yt-dlp`, and
`tesseract` with the English model (`tesseract-ocr-eng` on Debian/Ubuntu,
`tesseract-data-eng` on Arch).

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # points DATA_DIR / ARCHIVE_ROOT at ../data, ../media
uvicorn app.main:app --reload   # http://localhost:8000

# frontend (separate shell)
cd frontend
npm install
npm run dev                     # http://localhost:5173, proxies /api to :8000
```

### Tests

```bash
cd backend && pytest
```

Unit tests cover URL classification, match-window assembly, schedule alignment,
and VEX Events parsing. Integration tests (skipped without `ffmpeg`) drive real
frame sampling, clipping, range-request streaming, and the full OCR-only pipeline.

### Try the UI without a livestream

```bash
# make a synthetic broadcast-style recording
python scripts/make_sample_video.py sample.mp4 --matches 3
# ingest + process it (uses the same env as the app)
python scripts/seed_demo.py sample.mp4 --archive-subdir demo
```

## How detection works & its limits

- **Match windows**: frames are sampled (default 1 fps, tunable in Settings). Two
  signals are read per frame:
  1. the **match timer** — a run of frames showing a plausible, mostly-decreasing
     `M:SS` becomes a window; short blips are dropped, brief OCR dropouts bridged.
  2. the **match-number label** in the overlay header (`QUALIFICATION 13`) — a
     contiguous stretch where the label is visible, split when the number changes.
  The timer path wins when the clock actually counts down (≥4 distinct values);
  otherwise the label path is used. A `MIN/MAX_WINDOW_S` band filters non-matches.
- **Overlay profiles** (`backend/app/overlay_profiles/*.json`, plus user copies in
  `<data>/overlay_profiles/`) hold normalized rectangles for the match label,
  timer, team panels, and score panels. Two ship:
  - `vex_default` — a produced broadcast: timer centred near the top, alliance
    panels along the bottom third.
  - `vex_tm` — a **VEX Tournament Manager** audience-display / OBS overlay: label
    top-left of the header bar, clock top-right, alliance panels in the bottom
    corners. Common at regional and college events, including many **VEXU**
    streams. Set this in **Settings → overlay profile**.
  Broadcasts vary — if detection or OCR is off, adjust the rectangles for that
  event's layout (`PUT /api/overlay-profiles/{name}`) and re-process.
- **Alignment**: with a linked event, the k-th detected window maps to the k-th
  scheduled match (tolerating a leading offset and occasional misses). Teams and
  scores then come straight from VEX Events. Every match's boundaries, teams,
  and score remain editable in **Match Detail**, which re-cuts the clip on save.
- Pure-OCR results (no event link) are marked as such and are meant to be
  reviewed. **VEXU** in particular: link the VEX Events event — alphanumeric
  codes like `BLRS1` don't OCR reliably.
- **Streams with no usable overlay at all** (e.g. a bare multi-field camera grid
  with no graphics) can't be auto-split. Link the event and add match boundaries
  by hand in Match Detail, or record from a feed that carries the TM overlay.

## Key API endpoints

| Method & path | Purpose |
|---|---|
| `GET /api/auth/me` · `POST /api/auth/login` / `logout` / `setup` | session status, sign in/out, one-time first-admin setup |
| `GET/POST /api/users` · `PATCH/DELETE /api/users/{id}` | account management (create/delete: admin only) |
| `POST /api/recordings` | start a capture `{source_url, event?, archive_dir?, event_id?, live_from_start?, sections?}` — only `source_url` is required; `event` = events.vex.com link or event code (auto-imported); `sections` = `"HH:MM:SS-HH:MM:SS"`, archived video only |
| `POST /api/recordings/{id}/stop` / `/process` | stop capture / re-run the pipeline |
| `GET /api/matches?team=&event_id=&round=&sort=&order=&has_clip=` | library query |
| `PATCH /api/matches/{id}` | edit boundaries / teams / score (re-clips) |
| `GET /api/matches/{id}/clip` | range-request video stream |
| `GET /api/events/search?q=` · `POST /api/events/import` | VEX Events lookup/import by events.vex.com link or event code |
| `GET /api/health` | tool + OCR readiness |

Everything except `GET /api/matches…`, `GET /api/events`, `GET /api/health` and
`GET /api/auth/me` requires a signed-in session.

Interactive docs at `/docs`.
