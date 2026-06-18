# Avianca iCargo TRF007 Web Downloader

A Flask web app that runs the Avianca iCargo TRF007 Selenium workflow as a background job, merges the exported Excel files, and returns one downloadable workbook.

## What changed

- The web UI starts a background job instead of waiting on one long request.
- The page polls progress logs while Selenium runs.
- A running job can be cancelled with the Abort button.
- The fragile browser/login/TRF007 setup stage automatically retries once.
- Microsoft verification email search checks recent read and unread messages from the current challenge window.
- Selected airports and dates are passed into the downloader.
- Each job gets its own isolated download folder.
- Credentials are read from environment variables, not from source code.
- The merged file is served from `/api/jobs/<job_id>/file` when ready.

## Required environment variables

Set these locally in `.env` or directly in Railway:

```bash
AVIANCA_EMAIL=your-avianca-login@example.com
GMAIL_EMAIL=your-code-inbox@example.com
GMAIL_APP_PASSWORD=your-gmail-app-password
```

Optional:

```bash
HEADLESS=true
UPLOAD_TO_DROPBOX=false
DROPBOX_TOKEN=
DROPBOX_UPLOAD_PATH=/Cargo_Bookings
SEND_NOTIFICATION_EMAIL=false
JOB_TTL_HOURS=6
```

Use a Gmail app password, not your normal Gmail password.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`, then run:

```bash
set -a
source .env
set +a
python app.py
```

Open `http://localhost:5000`.

## Local CLI test

You can test the downloader without the web page:

```bash
set -a
source .env
set +a
python tariff_downloader.py run --airports HKG,MFM,XMN --start-date 2026-06-18 --end-date 2026-07-03 --download-dir /tmp/avianca-test
```

## Railway deployment

1. Push this folder to GitHub.
2. Create a Railway project from the GitHub repo.
3. Add the required environment variables in Railway.
4. Deploy.

This project includes a `Dockerfile`. Railway should use it automatically. That is preferred over Nixpacks because Selenium needs a matching Chromium and ChromeDriver inside the same Linux image.

After deploying, check:

```text
https://YOUR_DOMAIN/api/diagnostics
```

The Chrome section should show non-empty `chromiumPath` and `chromedriverPath`.

The `Procfile` uses:

```bash
python app.py
```

The app reads Railway's `PORT` value automatically.

## Important security cleanup

If real Gmail, Avianca, or Dropbox credentials were ever committed or shared, rotate them before deploying:

- Create a new Gmail app password.
- Revoke the exposed Dropbox token.
- Replace Railway/GitHub secrets with the new values.
- Remove old secrets from any public GitHub history if the repo was already pushed.

## Notes

- TRF007 is limited to a 15-day date range.
- Only one job runs at a time because the same account/MFA inbox is shared.
- Abort is cooperative: it stops at the next safe browser/email/download checkpoint.
- A cold Railway container or first Microsoft verification challenge may fail once; the app retries that setup stage automatically.
- If Gmail marks the Microsoft code email as read immediately, the app should still find it.
- The Gmail code search does not depend on recipient headers, which can be missing or rewritten by mail routing.
- Completed job files are kept for `JOB_TTL_HOURS`, then cleaned up when a new job starts.
- If ChromeDriver exits with status code `127`, Railway is missing a runnable system ChromeDriver. Redeploy with the included `nixpacks.toml` and check `/api/diagnostics`.
- If ChromeDriver exits with status code `127`, Railway is still not using the Docker image or system ChromeDriver correctly. Make sure the repo contains `Dockerfile`, then redeploy from the latest commit.
