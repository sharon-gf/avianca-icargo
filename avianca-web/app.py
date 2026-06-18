#!/usr/bin/env python3
"""
Avianca iCargo Tariff Downloader - Flask Web App
Handles download requests from the web interface
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import subprocess
import tempfile
import shutil

# ============================================================================
# SETUP
# ============================================================================

app = Flask(__name__)
CORS(app)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Temp directory for downloads
TEMP_DIR = Path(tempfile.gettempdir()) / "avianca_downloads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================================
# ROUTES
# ============================================================================

@app.route('/')
def index():
    """Serve the web interface"""
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def download():
    """
    Handle download request from frontend
    
    Expects JSON:
    {
        "module": "TRF007",
        "airports": ["CAN", "HKG", ...],
        "startDate": "2026-06-18",
        "endDate": "2026-07-03"
    }
    """
    try:
        data = request.get_json()
        
        module = data.get('module', 'TRF007')
        airports = data.get('airports', [])
        start_date = data.get('startDate')
        end_date = data.get('endDate')
        
        logger.info(f"Download request: {module}, {len(airports)} airports, {start_date} to {end_date}")
        
        if not airports:
            return jsonify({'error': 'No airports selected'}), 400
        
        if not start_date or not end_date:
            return jsonify({'error': 'Dates required'}), 400
        
        # Create temp work directory for this download
        work_dir = TEMP_DIR / f"download_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        work_dir.mkdir(parents=True, exist_ok=True)
        download_dir = work_dir / "files"
        download_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Working directory: {work_dir}")
        
        # Call the tariff downloader with parameters
        # Format: python tariff_downloader.py --module TRF007 --airports CAN,HKG --start-date 2026-06-18 --end-date 2026-07-03
        cmd = [
            sys.executable,
            'tariff_downloader.py',
            '--module', module,
            '--airports', ','.join(airports),
            '--start-date', start_date,
            '--end-date', end_date,
            '--output-dir', str(download_dir)
        ]
        
        logger.info(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        if result.returncode != 0:
            logger.error(f"Downloader failed: {result.stderr}")
            return jsonify({'error': 'Download failed: ' + result.stderr}), 500
        
        logger.info(f"Downloader output: {result.stdout}")
        
        # Find the merged file
        merged_files = list(download_dir.glob("merged_*.xlsx"))
        if not merged_files:
            logger.warning("No merged file found, checking for any xlsx files...")
            merged_files = list(download_dir.glob("*.xlsx"))
        
        if not merged_files:
            logger.error(f"No Excel files in {download_dir}")
            return jsonify({'error': 'No files were generated'}), 500
        
        merged_file = merged_files[0]
        logger.info(f"Merged file: {merged_file}")
        
        # Send file to client
        filename = f"TRF007_{len(airports)}airports_{start_date}_to_{end_date}.xlsx"
        
        @app.after_request
        def cleanup(response):
            # Schedule cleanup after file is sent
            try:
                import atexit
                atexit.register(lambda: shutil.rmtree(work_dir, ignore_errors=True))
            except:
                pass
            return response
        
        return send_file(
            str(merged_file),
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/api/status', methods=['GET'])
def status():
    """Health check"""
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Server error: {str(e)}")
    return jsonify({'error': 'Server error'}), 500

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
