from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import uuid
import time
import sqlite3
import datetime
import json
import shutil
import subprocess

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = 'downloads'
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

DB_FILE = 'log.db'


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        event_type TEXT,
        url TEXT,
        ip TEXT,
        status TEXT,
        details TEXT
    )
    ''')

    conn.commit()
    conn.close()


def log_event(event_type, url=None, status="success", details=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        timestamp = datetime.datetime.now().isoformat()

        ip = request.remote_addr or "unknown"

        if isinstance(details, dict):
            details = json.dumps(details)

        cursor.execute(
            "INSERT INTO logs (timestamp, event_type, url, ip, status, details) VALUES (?, ?, ?, ?, ?, ?)",
            (timestamp, event_type, url, ip, status, details)
        )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error logging event: {e}")


def extract_video_id(url):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get('id', 'unknown')


def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


@app.route('/api/info', methods=['POST'])
def get_video_info():
    data = request.json
    url = data.get('url')

    if not url:
        log_event("info_request", url=url, status="error", details="URL is required")
        return jsonify({'error': 'URL is required'}), 400

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'format': 'best',
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            formats = []
            for f in info.get('formats', []):
                # Only include formats with both video and audio
                if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                    size_mb = round(f.get('filesize', 0) / (1024 * 1024), 2) if f.get('filesize') else None

                    if size_mb is None and f.get('filesize_approx'):
                        size_mb = round(f.get('filesize_approx') / (1024 * 1024), 2)

                    if size_mb is None:
                        size_mb = 'Unknown'

                    formats.append({
                        'format_id': f.get('format_id'),
                        'resolution': f.get('resolution', 'Unknown'),
                        'ext': f.get('ext', 'mp4'),
                        'fps': f.get('fps', 'Unknown'),
                        'size_mb': size_mb,
                        'vcodec': f.get('vcodec', 'Unknown'),
                        'acodec': f.get('acodec', 'Unknown'),
                    })

            formats.sort(key=lambda x: (
                0 if x['resolution'] == 'Unknown' else
                int(x['resolution'].split('x')[1]) if 'x' in x['resolution'] else
                int(x['resolution'].rstrip('p')) if x['resolution'].endswith('p') else 0
            ), reverse=True)

            ffmpeg_available = check_ffmpeg()

            response_data = {
                'title': info.get('title', 'Unknown'),
                'author': info.get('uploader', 'Unknown'),
                'length_seconds': info.get('duration', 0),
                'thumbnail_url': info.get('thumbnail', ''),
                'streams': formats,
                'video_id': info.get('id', 'unknown'),
                'mp3_available': ffmpeg_available
            }

            log_event("info_request", url=url, details={
                "video_id": info.get('id', 'unknown'),
                "title": info.get('title', 'Unknown'),
                "stream_count": len(formats)
            })

            return jsonify(response_data)
    except Exception as e:
        error_msg = str(e)
        log_event("info_request", url=url, status="error", details=error_msg)
        return jsonify({'error': error_msg}), 500


@app.route('/api/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url')
    format_id = data.get('itag')  # Using 'itag' for compatibility with frontend

    if not url or not format_id:
        log_event("download_request", url=url, status="error", details="URL and format_id are required")
        return jsonify({'error': 'URL and format_id are required'}), 400

    try:
        download_id = uuid.uuid4().hex
        temp_dir = os.path.join(DOWNLOAD_FOLDER, f"temp_{download_id}")
        os.makedirs(temp_dir, exist_ok=True)

        ydl_opts = {
            'format': format_id,
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if 'entries' in info:  # It's a playlist
                info = info['entries'][0]

            downloaded_files = os.listdir(temp_dir)
            if not downloaded_files:
                raise Exception("Download failed: No files found")

            original_file = os.path.join(temp_dir, downloaded_files[0])
            file_ext = os.path.splitext(original_file)[1]
            final_filename = f"{download_id}{file_ext}"
            final_path = os.path.join(DOWNLOAD_FOLDER, final_filename)

            shutil.move(original_file, final_path)

            # Clean up temp directory
            shutil.rmtree(temp_dir)

            file_size_mb = round(os.path.getsize(final_path) / (1024 * 1024), 2)

            format_info = None
            resolution = "Unknown"
            for f in info.get('formats', []):
                if f.get('format_id') == format_id:
                    format_info = f
                    resolution = f.get('resolution', 'Unknown')
                    break

            # Log event
            log_event("download_request", url=url, details={
                "video_id": info.get('id', 'unknown'),
                "title": info.get('title', 'Unknown'),
                "format_id": format_id,
                "resolution": resolution,
                "file_size_mb": file_size_mb
            })

            # Return the download URL
            download_url = f"/api/file/{final_filename}"
            return jsonify({
                'download_url': download_url,
                'filename': info.get('title', 'video')
            })
    except Exception as e:
        error_msg = str(e)
        log_event("download_request", url=url, status="error", details=error_msg)
        return jsonify({'error': error_msg}), 500
    finally:
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except:
                pass


@app.route('/api/convert-to-mp3', methods=['POST'])
def convert_to_mp3():
    data = request.json
    url = data.get('url')
    format_id = data.get('itag')  # Using 'itag' for compatibility with frontend

    if not url:
        log_event("mp3_conversion", url=url, status="error", details="URL is required")
        return jsonify({'error': 'URL is required'}), 400

    if not check_ffmpeg():
        log_event("mp3_conversion", url=url, status="error", details="FFmpeg is not installed")
        return jsonify({'error': 'FFmpeg is not installed on the server. MP3 conversion is not available.'}), 500

    try:
        download_id = uuid.uuid4().hex
        temp_dir = os.path.join(DOWNLOAD_FOLDER, f"temp_{download_id}")
        os.makedirs(temp_dir, exist_ok=True)

        ydl_opts = {
            'format': 'bestaudio/best' if not format_id else format_id,
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if 'entries' in info:  # It's a playlist
                info = info['entries'][0]

            downloaded_files = os.listdir(temp_dir)
            if not downloaded_files:
                raise Exception("Download failed: No files found")

            original_file = os.path.join(temp_dir, downloaded_files[0])
            mp3_filename = f"{download_id}.mp3"
            mp3_path = os.path.join(DOWNLOAD_FOLDER, mp3_filename)

            cmd = [
                'ffmpeg',
                '-i', original_file,
                '-vn',  # No video
                '-ar', '44100',  # Audio sampling rate
                '-ac', '2',  # Stereo
                '-b:a', '192k',  # Bitrate
                '-f', 'mp3',
                mp3_path
            ]

            process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            if process.returncode != 0:
                raise Exception(f"FFmpeg conversion failed: {process.stderr.decode()}")

            shutil.rmtree(temp_dir)

            file_size_mb = round(os.path.getsize(mp3_path) / (1024 * 1024), 2)

            log_event("mp3_conversion", url=url, details={
                "video_id": info.get('id', 'unknown'),
                "title": info.get('title', 'Unknown'),
                "file_size_mb": file_size_mb
            })

            download_url = f"/api/file/{mp3_filename}"
            return jsonify({
                'download_url': download_url,
                'filename': f"{info.get('title', 'audio')}.mp3"
            })
    except Exception as e:
        error_msg = str(e)
        log_event("mp3_conversion", url=url, status="error", details=error_msg)
        return jsonify({'error': error_msg}), 500
    finally:
        # Make sure temp directory is cleaned up in case of errors
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except:
                pass


@app.route('/api/file/<filename>', methods=['GET'])
def get_file(filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, filename)
    if os.path.exists(file_path):
        log_event("file_access", details={"filename": filename})
        return send_file(file_path, as_attachment=True)
    else:
        log_event("file_access", status="error", details=f"File not found: {filename}")
        return jsonify({'error': 'File not found'}), 404


def cleanup_downloads():
    while True:
        now = time.time()
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            if not os.path.isfile(file_path):
                continue

            if now - os.path.getmtime(file_path) > 3600:
                try:
                    os.remove(file_path)
                    log_event("file_cleanup", details={"filename": filename})
                except Exception as e:
                    log_event("file_cleanup", status="error", details=f"Error removing {filename}: {str(e)}")
        time.sleep(3600)


if __name__ == '__main__':
    init_db()

    import threading

    cleanup_thread = threading.Thread(target=cleanup_downloads)
    cleanup_thread.daemon = True
    cleanup_thread.start()

    with app.app_context():
        log_event("application_start", details={
            "version": "1.1.0",
            "backend": "yt-dlp",
            "ffmpeg_available": check_ffmpeg()
        })

    app.run(debug=True, host='0.0.0.0', port=5000)
