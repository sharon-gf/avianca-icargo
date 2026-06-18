# Avianca iCargo Tariff Downloader - Web Portal

A web interface for downloading and merging tariff data from Avianca iCargo.

## Features

- 🌐 Web-based interface (no terminal needed)
- ✈️ Select specific airports or all 27
- 📅 Choose date ranges
- 📊 Automatic file merging
- ☁️ Deploy to Railway in 2 minutes

## Setup for Railway

### 1. Create GitHub repo

```bash
cd avianca-web
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/avianca-web.git
git push -u origin main
```

### 2. Deploy to Railway

1. Go to [Railway.app](https://railway.app)
2. Click "New Project"
3. Select "Deploy from GitHub repo"
4. Select `avianca-web` repo
5. Railway will auto-detect Flask and deploy!

### 3. Configure environment variables in Railway

In Railway dashboard, add these variables:

```
AVIANCA_EMAIL=ldiaz@gocargogsa.com
GMAIL_PASSWORD=ayph ojlv hnrt osme
DROPBOX_TOKEN=your_dropbox_token
```

### 4. Your team visits the URL

Railway gives you a URL like: `https://avianca-web-production.up.railway.app`

Your team opens it in a browser, selects airports/dates, clicks download!

## Local development

```bash
python -m venv venv
source venv/bin/activate  # or: venv\Scripts\activate on Windows
pip install -r requirements.txt
python app.py
```

Then visit: `http://localhost:5000`

## How it works

1. User selects module, airports, and date range in the web interface
2. Flask backend receives the request
3. Calls `tariff_downloader.py` with those parameters
4. Downloader logs into Avianca, downloads tariffs for selected airports
5. Files are merged into one Excel file
6. File is sent to user's computer as download

## File structure

```
avianca-web/
├── app.py                    # Flask backend
├── tariff_downloader.py      # Your existing downloader script
├── requirements.txt          # Python dependencies
├── railway.toml             # Railway config
├── templates/
│   └── index.html           # Web interface
└── .gitignore
```

## Troubleshooting

**"Chrome driver not found"** → Railway needs Chromium installed. This is handled automatically by nixpacks.

**"Email verification times out"** → Check GMAIL_PASSWORD and IMAP settings

**"Large files timeout"** → Railway has 120s timeout. You may need to increase or use async.

## Next steps

- Add support for other modules (BKG001, AWB001, etc.)
- Add authentication for your team
- Add download history/logging
- Store merged files in cloud storage (Dropbox, S3)
