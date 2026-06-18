#!/usr/bin/env python3
"""
Avianca iCargo Tariff Downloader - Flask Web App.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

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
]
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
        job["logs"].append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
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
        "airports": job["airports"],
        "startDate": job["start_date"],
        "endDate": job["end_date"],
        "error": job.get("error"),
        "result": job.get("result"),
    }
    if job["status"] == "completed":
        response["downloadUrl"] = f"/api/jobs/{job['id']}/file"
    return response


def find_active_job_locked() -> dict | None:
    for job in JOBS.values():
        if job["status"] in {"queued", "running"}:
            return job
    return None


def cleanup_old_jobs() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=JOB_TTL_HOURS)
    stale_jobs = []

    with JOB_LOCK:
        for job_id, job in list(JOBS.items()):
            if job["status"] in {"queued", "running"}:
                continue
            updated_at = datetime.fromisoformat(job["updated_at"])
            if updated_at < cutoff:
                stale_jobs.append((job_id, Path(job["work_dir"])))
                JOBS.pop(job_id, None)

    for _, work_dir in stale_jobs:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_job(job_id: str) -> None:
    from tariff_downloader import run_download_workflow

    with JOB_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["progress"] = 1
        job["updated_at"] = now_iso()
        airports = list(job["airports"])
        start_date = job["start_date"]
        end_date = job["end_date"]
        download_dir = Path(job["download_dir"])

    add_job_log(job_id, "Job started", 1)

    try:
        result = run_download_workflow(
            airports=airports,
            start_date=start_date,
            end_date=end_date,
            download_dir=download_dir,
            upload_dropbox=env_flag("UPLOAD_TO_DROPBOX"),
            send_email=env_flag("SEND_NOTIFICATION_EMAIL"),
            progress_callback=lambda message, progress=None: add_job_log(job_id, message, progress),
        )

        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "completed"
            job["progress"] = 100
            job["result"] = {
                "successfulDownloads": result["successful_downloads"],
                "failedDownloads": result["failed_downloads"],
                "fileName": Path(result["merged_file"]).name,
            }
            job["file_path"] = result["merged_file"]
            job["updated_at"] = now_iso()
        add_job_log(job_id, "Merged file is ready", 100)

    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["error"] = str(exc)
            job["updated_at"] = now_iso()
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


@app.route("/api/download", methods=["POST"])
def start_download():
    cleanup_old_jobs()

    try:
        data = request.get_json(silent=True) or {}
        module = data.get("module", "TRF007")
        airports = [str(code).strip().upper() for code in data.get("airports", []) if str(code).strip()]
        start_date = data.get("startDate")
        end_date = data.get("endDate")

        if module != "TRF007":
            return jsonify({"error": "Only TRF007 is currently supported"}), 400
        if not airports:
            return jsonify({"error": "No airports selected"}), 400

        allowed_airports = set(DEFAULT_AIRPORTS)
        invalid_airports = sorted(set(airports) - allowed_airports)
        if invalid_airports:
            return jsonify({"error": f"Invalid airports: {', '.join(invalid_airports)}"}), 400

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

            JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "logs": [],
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "airports": airports,
                "start_date": start_date,
                "end_date": end_date,
                "work_dir": str(work_dir),
                "download_dir": str(download_dir),
                "file_path": None,
                "result": None,
                "error": None,
            }

        EXECUTOR.submit(run_job, job_id)
        with JOB_LOCK:
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


@app.route("/api/jobs/<job_id>/file")
def download_job_file(job_id: str):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] != "completed":
            return jsonify({"error": "Job is not complete yet"}), 409
        file_path = Path(job["file_path"])
        filename = (
            f"TRF007_{job['result']['successfulDownloads']}airports_"
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


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(error):
    logger.error("Server error: %s", error)
    return jsonify({"error": "Server error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting on port %s", port)
    app.run(host="0.0.0.0", port=port, debug=False)
