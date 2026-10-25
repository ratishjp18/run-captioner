# run-titler

Automatically renames your Strava runs with short, fun titles using OpenAI.

A FastAPI webhook server receives new-activity events from Strava, waits for GPS processing to finish, sends a compact run summary to OpenAI, and writes the generated title back.

> **Fair warning:** This automates personal data in a way that technically bends Strava's API agreement. It's a private project with ~3 runs a week, so enforcement risk is low — but build it expecting it might stop working some day.

> **Strava API URL change:** Strava is moving the API base to `api-v3.strava.com` on **January 4, 2027**. When that happens, update `STRAVA_BASE_URL` in your `.env` (or Render config).

---

## How it works

1. You finish a run; Strava processes the GPS data
2. Strava POSTs a webhook event to your server
3. The server waits 30–60 s (Strava needs a moment), then fetches the activity
4. A compact summary (distance, pace, elevation, time of day) is sent to OpenAI — no GPS coordinates, no HR streams, no name
5. The new title is written back with `PUT /activities/{id}`

Only activities whose current title is one of Strava's defaults (`"Morning Run"`, `"Afternoon Run"`, etc.) are renamed. Titles you set yourself are never touched.

---

## Prerequisites

| Thing | Where to get it |
|---|---|
| Python 3.12+ | [python.org](https://python.org) |
| Strava API app | [strava.com/settings/api](https://www.strava.com/settings/api) |
| OpenAI API key | [platform.openai.com](https://platform.openai.com) |
| Public HTTPS URL | ngrok (local), Render or Railway (production) |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create the Strava API app

1. Go to <https://www.strava.com/settings/api>
2. Create a new app — name and website don't matter for personal use
3. Note the **Client ID** and **Client Secret**

### 3. Configure your environment

```bash
cp .env.example .env
```

Open `.env` and fill in:
- `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` from step 2
- `STRAVA_VERIFY_TOKEN` — pick any random secret string (or the script will generate one)
- `OPENAI_API_KEY`

Leave `STRAVA_REFRESH_TOKEN` and `ATHLETE_ID` blank for now.

### 4. Get a public HTTPS URL

**For local testing with ngrok:**

```bash
# Install ngrok from https://ngrok.com/download, then:
ngrok http 8000
# Copy the https://... URL it prints
```

**For production:** deploy first (see Render section below) and use that URL.

### 5. Run the OAuth + webhook setup script

```bash
python setup_oauth.py --callback-url https://YOUR_URL/webhook
```

The script will:
- Open your browser to Strava's authorization page
- Exchange the code for tokens
- **Print the values to add to `.env`** — copy them in now
- Register the webhook subscription with Strava (your server must be running and reachable)

> To check an existing webhook: `python setup_oauth.py --callback-url https://x.com/webhook --list-only`

### 6. Start the server

```bash
uvicorn main:app --reload
```

Or with Docker:

```bash
docker-compose up -d
docker-compose logs -f
```

---

## Local testing workflow

1. Start ngrok: `ngrok http 8000`
2. Start the server: `uvicorn main:app`
3. Run setup with the ngrok URL (step 5 above)
4. In `.env`, set `DRY_RUN=true` — titles are logged but not written to Strava
5. Go to Strava and **manually create an activity** with a default title like `"Morning Run"`
6. Watch the logs — you should see the webhook arrive, a ~45 s wait, and the generated title
7. Once happy with the output, set `DRY_RUN=false`

**Never test on your next real run first.** Use a manual or old activity.

---

## Deploying to Render

1. Verify `.env` is in `.gitignore` before pushing:
   ```bash
   git status  # .env should NOT appear
   ```
2. Push to GitHub
3. Create a new **Web Service** on [Render](https://render.com)
   - Connect your GitHub repo
   - Runtime: **Docker**
   - Health Check Path: `/health`
4. Add all variables from `.env.example` in Render's **Environment** tab
5. Deploy, then update the webhook with the Render URL:
   ```bash
   python setup_oauth.py \
     --callback-url https://YOUR-APP.onrender.com/webhook \
     --skip-oauth
   ```

Render's free tier sleeps after 15 minutes of inactivity. Use a paid plan or Railway to avoid a cold-start delay when the first webhook arrives.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `STRAVA_CLIENT_ID` | ✓ | — | Strava app client ID |
| `STRAVA_CLIENT_SECRET` | ✓ | — | Strava app client secret |
| `STRAVA_REFRESH_TOKEN` | ✓ | — | Bootstrap refresh token (from setup script) |
| `STRAVA_VERIFY_TOKEN` | ✓ | — | Shared secret for webhook validation |
| `ATHLETE_ID` | ✓ | — | Your Strava athlete ID (from setup script) |
| `OPENAI_API_KEY` | ✓ | — | OpenAI API key |
| `OPENAI_MODEL` | | `gpt-4o-mini` | Model name — swap here if you want a different model |
| `DRY_RUN` | | `false` | `true` = log title only, don't write to Strava |
| `STRAVA_BASE_URL` | | `https://www.strava.com/api/v3` | Update after Jan 4 2027 |
| `DB_PATH` | | `run_titler.db` | SQLite path — override in Docker to a volume path |

---

## Estimated cost

`gpt-4o-mini` at 3 runs/week:

- ~150 tokens input + ~15 tokens output per run
- ≈ 500 input + 60 output tokens/week
- At $0.15/M input, $0.60/M output → **< $0.01/month**

---

## Safety details

- **Never overwrites custom titles** — only renames if the current title is one of Strava's defaults
- **Deduplication** — each activity ID is stored in SQLite; the same activity is never processed twice
- **Update loop prevention** — the webhook handler only acts on `aspect_type == "create"`, not `"update"`, so your own rename doesn't trigger a loop
- **Data minimisation** — only activity IDs, basic stats, and tokens are stored; records older than 7 days are deleted automatically
- **Secrets** — never logged, never committed (`.env` and `*.db` are in `.gitignore`)

---

## Troubleshooting

**Webhook not firing**
- Confirm the subscription: `python setup_oauth.py --list-only --callback-url x`
- Check the server is publicly reachable at the registered URL

**Token errors**
- The app auto-refreshes tokens every 6 hours. If the DB is lost, re-run `setup_oauth.py` and update `STRAVA_REFRESH_TOKEN` in `.env`

**Title not changing**
- Check the logs — the most common reason is the activity already had a custom title

**Activity data not ready (repeated retries in logs)**
- Normal for manual activities with no GPS. The app will give up after 4 attempts

**Database issues**
- Delete `run_titler.db` and restart. You'll lose dedup history (safe) and tokens (re-run setup)
