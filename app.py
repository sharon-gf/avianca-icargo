#!/usr/bin/env python3
"""
Avianca iCargo Tariff Downloader - Flask Web App
Full version with Selenium support for Railway Pro
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file
import subprocess
import tempfile
import shutil

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

@app.route('/api/status')
def status():
    """Health check"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/download', methods=['POST'])
def download():
    """
    Handle download request from frontend
    """
    try:
        data = request.get_json()
        
        airports = data.get('airports', [])
        start_date = data.get('startDate')
        end_date = data.get('endDate')
        
        logger.info(f"Download request: {len(airports)} airports, {start_date} to {end_date}")
        
        if not airports:
            return jsonify({'error': 'No airports selected'}), 400
        
        if not start_date or not end_date:
            return jsonify({'error': 'Dates required'}), 400
        
        # Create temp work directory
        work_dir = TEMP_DIR / f"download_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        work_dir.mkdir(parents=True, exist_ok=True)
        download_dir = work_dir / "files"
        download_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Working directory: {work_dir}")
        logger.info(f"Download dir: {download_dir}")
        
        # Set environment variable for download directory
        env = os.environ.copy()
        env['DOWNLOAD_DIR'] = str(download_dir)
        
        # Run the downloader - just "run" command, no extra arguments
        # The script will use CONFIG settings
        cmd = [sys.executable, 'tariff_downloader.py', 'run']
        
        logger.info(f"Running downloader: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
        
        logger.info(f"Downloader return code: {result.returncode}")
        logger.info(f"Downloader output: {result.stdout[:500]}")
        
        if result.returncode != 0:
            logger.error(f"Downloader stderr: {result.stderr[:500]}")
            return jsonify({'error': f'Download failed'}), 500
        
        # Find merged file
        logger.info(f"Looking for files in {download_dir}")
        merged_files = list(download_dir.glob("*.xlsx"))
        
        logger.info(f"Found {len(merged_files)} Excel files")
        
        if not merged_files:
            logger.error(f"No Excel files in {download_dir}")
            return jsonify({'error': 'No files were generated'}), 500
        
        merged_file = sorted(merged_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]
        logger.info(f"Using file: {merged_file}")
        
        # Send file
        filename = f"TRF007_{len(airports)}airports_{start_date}_to_{end_date}.xlsx"
        
        try:
            return send_file(
                str(merged_file),
                as_attachment=True,
                download_name=filename,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        finally:
            # Cleanup after
            import atexit
            atexit.register(lambda: shutil.rmtree(work_dir, ignore_errors=True))
    
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

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
    logger.info(f"Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
