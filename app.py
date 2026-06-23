#!/usr/bin/env python3
"""
Avianca iCargo Tariff Downloader - Flask Web App.
"""

from __future__ import annotations

import logging
import os
import importlib
import re
import shutil
import tempfile
import threading
import uuid
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file

DEFAULT_AIRPORTS = [
    "CAN",
    "HKG",
    "NGO",
    "ISB",
    "XMN",
    "CGK",
    "ICN",
    "TPE",
    "CGO",
    "DPS",
    "GMP",
    "HAN",
    "PEK",
    "NRT",
    "MFM",
    "SGN",
    "PVG",
    "HND",
    "KHI",
    "DAD",
    "SZX",
    "KIX",
    "LHE",
    "MIA",
]
CAP142_COUNTRY_ORIGINS = {"CN", "HK", "TW", "JP", "KR", "VN", "ID"}
MAX_RANGE_DAYS = 15


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TEMP_DIR = Path(os.getenv("JOB_ROOT", tempfile.gettempdir())) / "avianca_downloads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

JOB_TTL_HOURS = int(os.getenv("JOB_TTL_HOURS", "6"))
JOB_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}

# Use one worker because the same iCargo/Gmail login should not run multiple MFA
# sessions at once.
EXECUTOR = ThreadPoolExecutor(max_workers=1)
CLIENT_VERSION = "job-api-v2"
APP_BUILD_VERSION = "job-api-v24-cap142-country-origin"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def parse_iso_date(value: str, field_name: str) -> datetime.date:
    if not value:
        raise ValueError(f"{field_name} is required")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format") from exc


def add_job_log(job_id: str, message: str, progress: int | None = None) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        timestamp = datetime.now(timezone.utc)
        job["logs"].append(
            {
                "time": timestamp.strftime("%H:%M:%S"),
                "timeUtc": timestamp.isoformat(),
                "message": message,
            }
        )
        job["logs"] = job["logs"][-300:]
        if progress is not None:
            job["progress"] = max(0, min(100, int(progress)))
        job["updated_at"] = now_iso()


def public_job(job: dict) -> dict:
    response = {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "logs": job["logs"],
        "createdAt": job["created_at"],
        "updatedAt": job["updated_at"],
        "module": job.get("module", "TRF007"),
        "cap142Options": job.get("cap142_options") or {},
        "airports": job["airports"],
        "startDate": job["start_date"],
        "endDate": job["end_date"],
        "error": job.get("error"),
        "result": job.get("result"),
        "debugFiles": public_debug_files(job),
        "cancelRequested": bool(job.get("cancel_requested")),
    }
    if job["status"] == "completed":
        response["downloadUrl"] = f"/api/jobs/{job['id']}/file"
    return response


def public_debug_files(job: dict) -> list[dict]:
    debug_dir = Path(job["work_dir"]) / "debug"
    if not debug_dir.exists():
        return []

    files = []
    for path in sorted(debug_dir.iterdir(), key=lambda item: item.stat().st_mtime):
        if not path.is_file() or path.suffix.lower() not in {".png", ".html"}:
            continue
        files.append(
            {
                "name": path.name,
                "url": f"/api/jobs/{job['id']}/debug/{path.name}",
            }
        )
    return files[-10:]


def find_active_job_locked() -> dict | None:
    for job in JOBS.values():
        if job["status"] in {"queued", "running", "cancelling"}:
            return job
    return None


def cleanup_old_jobs() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=JOB_TTL_HOURS)
    stale_jobs = []

    with JOB_LOCK:
        for job_id, job in list(JOBS.items()):
            if job["status"] in {"queued", "running", "cancelling"}:
                continue
            updated_at = datetime.fromisoformat(job["updated_at"])
            if updated_at < cutoff:
                stale_jobs.append((job_id, Path(job["work_dir"])))
                JOBS.pop(job_id, None)

    for _, work_dir in stale_jobs:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.after_request
def disable_response_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    if request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip() == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.before_request
def redirect_http_to_https():
    forwarded_proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    if forwarded_proto == "http" and request.host == "icargo.gsaforce.com":
        return redirect(request.url.replace("http://", "https://", 1), code=301)


def load_downloader_function():
    try:
        downloader = importlib.import_module("tariff_downloader")
    except Exception as exc:
        raise RuntimeError(f"Could not import tariff_downloader.py: {exc}") from exc

    workflow = getattr(downloader, "run_download_workflow", None)
    if not callable(workflow):
        source_path = getattr(downloader, "__file__", "unknown path")
        build_version = getattr(downloader, "DOWNLOADER_BUILD_VERSION", "missing")
        raise RuntimeError(
            "Railway is running the wrong tariff_downloader.py. "
            f"Expected run_download_workflow, but it was not found in {source_path}. "
            f"Downloader build marker: {build_version}. "
            "Push/redeploy the latest tariff_downloader.py from the fixed project folder."
        )

    return workflow


def run_job(job_id: str) -> None:
    with JOB_LOCK:
        job = JOBS[job_id]
        if job.get("cancel_requested"):
            job["status"] = "cancelled"
            job["progress"] = 0
            job["updated_at"] = now_iso()
            return

        job["status"] = "running"
        job["progress"] = 1
        job["updated_at"] = now_iso()
        airports = list(job["airports"])
        start_date = job["start_date"]
        end_date = job["end_date"]
        module = job.get("module", "TRF007")
        cap142_options = dict(job.get("cap142_options") or {})
        download_dir = Path(job["download_dir"])
        cancel_event = job["cancel_event"]

    add_job_log(job_id, "Job started", 1)

    try:
        run_download_workflow = load_downloader_function()

        result = run_download_workflow(
            airports=airports,
            start_date=start_date,
            end_date=end_date,
            download_dir=download_dir,
            upload_dropbox=env_flag("UPLOAD_TO_DROPBOX"),
            send_email=env_flag("SEND_NOTIFICATION_EMAIL"),
            progress_callback=lambda message, progress=None: add_job_log(job_id, message, progress),
            cancel_callback=cancel_event.is_set,
            module=module,
            **cap142_options,
        )

        with JOB_LOCK:
            job = JOBS[job_id]
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["updated_at"] = now_iso()
                return

            job["status"] = "completed"
            job["progress"] = 100
            job["result"] = {
                "successfulDownloads": result["successful_downloads"],
                "failedDownloads": result["failed_downloads"],
                "fileName": Path(result["merged_file"]).name,
                "module": result.get("module", module),
            }
            job["file_path"] = result["merged_file"]
            job["updated_at"] = now_iso()
        add_job_log(job_id, "Merged file is ready", 100)

    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        with JOB_LOCK:
            job = JOBS[job_id]
            if job.get("cancel_requested"):
                job["status"] = "cancelled"
                job["error"] = None
            else:
                job["status"] = "failed"
                job["error"] = str(exc)
            job["updated_at"] = now_iso()
        if cancel_event.is_set():
            add_job_log(job_id, "Job cancelled")
        else:
            add_job_log(job_id, f"Job failed: {exc}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    with JOB_LOCK:
        active_job = find_active_job_locked()
        active_job_id = active_job["id"] if active_job else None
    return jsonify({"status": "ok", "timestamp": now_iso(), "activeJobId": active_job_id})


@app.route("/api/version")
def version():
    return jsonify(
        {
            "appBuildVersion": APP_BUILD_VERSION,
            "clientVersion": CLIENT_VERSION,
            "routes": registered_routes(),
        }
    )


@app.route("/api/diagnostics")
def diagnostics():
    downloader_info = {
        "importOk": False,
        "path": None,
        "buildVersion": None,
        "hasRunDownloadWorkflow": False,
        "error": None,
    }

    try:
        downloader = importlib.import_module("tariff_downloader")
        downloader_info.update(
            {
                "importOk": True,
                "path": getattr(downloader, "__file__", None),
                "buildVersion": getattr(downloader, "DOWNLOADER_BUILD_VERSION", "missing"),
                "compareQuery": getattr(downloader, "CSPROD_QUERY_NAME", "missing"),
                "hasRunDownloadWorkflow": callable(getattr(downloader, "run_download_workflow", None)),
            }
        )
    except Exception as exc:
        downloader_info["error"] = str(exc)

    return jsonify(
        {
            "appBuildVersion": APP_BUILD_VERSION,
            "clientVersion": CLIENT_VERSION,
            "downloader": downloader_info,
            "environment": {
                "hasAviancaEmail": bool(os.getenv("AVIANCA_EMAIL")),
                "hasGmailEmail": bool(os.getenv("GMAIL_EMAIL")),
                "hasGmailAppPassword": bool(os.getenv("GMAIL_APP_PASSWORD") or os.getenv("GMAIL_PASSWORD")),
                "hasCsprodUsername": bool(os.getenv("CSPROD_USERNAME")),
                "hasCsprodPassword": bool(os.getenv("CSPROD_PASSWORD")),
            },
            "chrome": binary_diagnostics(),
        }
    )


def command_version(command: str | None) -> str | None:
    if not command:
        return None
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = (result.stdout or result.stderr).strip()
        return output or f"exit code {result.returncode}"
    except Exception as exc:
        return f"error: {exc}"


def binary_diagnostics() -> dict:
    chromium_path = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    chromedriver_path = shutil.which("chromedriver")
    return {
        "chromeBinEnv": os.getenv("CHROME_BIN") or os.getenv("GOOGLE_CHROME_BIN"),
        "chromiumPath": chromium_path,
        "chromiumVersion": command_version(chromium_path),
        "chromedriverPath": chromedriver_path,
        "chromedriverVersion": command_version(chromedriver_path),
    }


def registered_routes() -> list[str]:
    return sorted(rule.rule for rule in app.url_map.iter_rules())


@app.route("/api/download", methods=["POST"])
def start_download():
    cleanup_old_jobs()

    try:
        data = request.get_json(silent=True) or {}
        if request.headers.get("X-Client-Version") != CLIENT_VERSION:
            return (
                jsonify({"error": "Please refresh the page before starting a download."}),
                426,
            )

        module = str(data.get("module", "TRF007")).strip().upper()
        airports = [str(code).strip().upper() for code in data.get("airports", []) if str(code).strip()]
        start_date = data.get("startDate")
        end_date = data.get("endDate")
        cap142_options = {}
        cap142_mode = str(data.get("cap142Mode", "booking_period")).strip().lower()
        compare_with_system = bool(data.get("compareWithSystem"))

        if module not in {"TRF007", "CAP142"}:
            return jsonify({"error": "Only TRF007 and CAP142 are currently supported"}), 400
        if module == "CAP142" and cap142_mode not in {"specific_flight", "booking_period"}:
            return jsonify({"error": "CAP142 mode must be booking_period or specific_flight"}), 400

        origin_type = "Airport"
        if module == "CAP142":
            origin_type_key = str(data.get("originType", "Airport")).strip().lower() or "airport"
            if origin_type_key not in {"airport", "country"}:
                return jsonify({"error": "CAP142 origin type must be Airport or Country"}), 400
            origin_type = "Country" if origin_type_key == "country" else "Airport"

        origin_is_optional = module == "CAP142" and cap142_mode == "specific_flight"
        if not airports and not origin_is_optional:
            return jsonify({"error": "No origins selected"}), 400

        if module == "CAP142" and origin_type == "Country":
            invalid_origins = sorted(set(airports) - CAP142_COUNTRY_ORIGINS)
            if invalid_origins:
                return jsonify({"error": f"Invalid CAP142 country origins: {', '.join(invalid_origins)}"}), 400
        else:
            allowed_airports = set(DEFAULT_AIRPORTS)
            invalid_airports = sorted(set(airports) - allowed_airports)
            if invalid_airports:
                return jsonify({"error": f"Invalid airports: {', '.join(invalid_airports)}"}), 400

        if module == "CAP142":
            if compare_with_system and cap142_mode != "specific_flight":
                return jsonify({"error": "Download and compare is only available for CAP142 specific flight for now."}), 400

            if cap142_mode == "specific_flight":
                awb_prefix = ""
                flight_carrier = str(data.get("flightCarrier", "QT")).strip().upper() or "QT"
                flight_number = re.sub(r"\D", "", str(data.get("flightNumber", "")))
            else:
                awb_prefix = re.sub(r"\D", "", str(data.get("awbPrefix", "729"))) or "729"
                flight_carrier = ""
                flight_number = ""
            if cap142_mode == "specific_flight":
                if len(airports) > 1:
                    return jsonify({"error": "Specific flight mode can use one origin or no origin filter"}), 400
                if not flight_number:
                    return jsonify({"error": "Flight number is required for CAP142 specific flight"}), 400

            cap142_options = {
                "cap142_mode": cap142_mode,
                "flight_carrier": flight_carrier,
                "flight_number": flight_number,
                "awb_prefix": awb_prefix,
                "origin_type": origin_type,
                "compare_with_system": compare_with_system,
            }
        elif compare_with_system:
            return jsonify({"error": "Download and compare is only available for CAP142 specific flight for now."}), 400

        start = parse_iso_date(start_date, "startDate")
        end = parse_iso_date(end_date, "endDate")
        days = (end - start).days
        if days < 0:
            return jsonify({"error": "End date must be on or after start date"}), 400
        if days > MAX_RANGE_DAYS:
            return jsonify({"error": f"Date range cannot exceed {MAX_RANGE_DAYS} days"}), 400

        with JOB_LOCK:
            active_job = find_active_job_locked()
            if active_job:
                return (
                    jsonify(
                        {
                            "error": "Another download is already running. Please wait for it to finish.",
                            "activeJob": public_job(active_job),
                        }
                    ),
                    409,
                )

            job_id = uuid.uuid4().hex
            work_dir = TEMP_DIR / job_id
            download_dir = work_dir / "files"
            download_dir.mkdir(parents=True, exist_ok=True)
            cancel_event = threading.Event()

            JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "logs": [],
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "airports": airports,
                "module": module,
                "cap142_options": cap142_options,
                "start_date": start_date,
                "end_date": end_date,
                "work_dir": str(work_dir),
                "download_dir": str(download_dir),
                "file_path": None,
                "result": None,
                "error": None,
                "cancel_requested": False,
                "cancel_event": cancel_event,
                "future": None,
            }

        with JOB_LOCK:
            JOBS[job_id]["future"] = EXECUTOR.submit(run_job, job_id)
            job = public_job(JOBS[job_id])
        return jsonify(job), 202

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Could not start download")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/jobs/<job_id>")
def get_job(job_id: str):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(public_job(job))


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id: str):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        if job["status"] in {"completed", "failed", "cancelled"}:
            return jsonify(public_job(job))

        job["cancel_requested"] = True
        job["cancel_event"].set()
        future = job.get("future")
        if job["status"] == "queued" and future and future.cancel():
            job["status"] = "cancelled"
        else:
            job["status"] = "cancelling"

        job["updated_at"] = now_iso()

    add_job_log(job_id, "Abort requested")
    with JOB_LOCK:
        return jsonify(public_job(JOBS[job_id]))


@app.route("/api/jobs/<job_id>/file")
def download_job_file(job_id: str):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] != "completed":
            return jsonify({"error": "Job is not complete yet"}), 409
        file_path = Path(job["file_path"])
        module = job.get("module", "TRF007")
        unit = "origins" if module == "CAP142" else "airports"
        filename = (
            f"{module}_{job['result']['successfulDownloads']}{unit}_"
            f"{job['start_date']}_to_{job['end_date']}.xlsx"
        )

    if not file_path.exists():
        return jsonify({"error": "Generated file is no longer available"}), 410

    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/jobs/<job_id>/debug/<filename>")
def download_debug_file(job_id: str, filename: str):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        debug_dir = (Path(job["work_dir"]) / "debug").resolve()

    file_path = (debug_dir / filename).resolve()
    if file_path.parent != debug_dir or not file_path.exists() or file_path.suffix.lower() not in {".png", ".html"}:
        return jsonify({"error": "Debug file not found"}), 404

    return send_file(str(file_path), as_attachment=False)


@app.errorhandler(404)
def not_found(_):
    return (
        jsonify(
            {
                "error": "Not found",
                "appBuildVersion": APP_BUILD_VERSION,
                "routes": registered_routes(),
            }
        ),
        404,
    )


@app.errorhandler(500)
def server_error(error):
    logger.error("Server error: %s", error)
    return jsonify({"error": "Server error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting on port %s", port)
    logger.info("App build version: %s", APP_BUILD_VERSION)
    logger.info("Registered routes: %s", ", ".join(registered_routes()))
    app.run(host="0.0.0.0", port=port, debug=False)
