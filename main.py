from flask import Flask, render_template, request, send_file, jsonify
from yt_dlp import YoutubeDL
import os
import re
import threading
import uuid
import time
import logging
from datetime import datetime
from urllib.parse import unquote
import glob

app = Flask(__name__)
app.config['DOWNLOAD_FOLDER'] = 'downloads'
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1GB
app.logger.setLevel(logging.DEBUG)

os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)

download_queue = []
current_download = None
download_history = []
lock = threading.Lock()

def sanitize_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title).strip()[:200]

def is_youtube_url(url):
    youtube_pattern = (
        r'(https?://)?(www\.)?'
        '(youtube\.com|youtu\.be|youtube-nocookie\.com)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'
    )
    return re.match(youtube_pattern, url)

@app.errorhandler(404)
@app.errorhandler(500)
def json_error_handler(e):
    response = {
        'error': str(e.description if hasattr(e, 'description') else e)
    }
    return jsonify(response), e.code if hasattr(e, 'code') else 500

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_info', methods=['POST'])
def get_info():
    if not request.is_json:
        return jsonify({'error': 'Expected JSON input'}), 400
    try:
        url = request.json['url']
        app.logger.info(f"Fetching info for URL: {url}")
        if not is_youtube_url(url):
            return jsonify({'error': 'Only YouTube links are allowed'}), 400

        with YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = {'video': [], 'audio': []}

            for f in info['formats']:
                if f.get('vcodec') != 'none' and f.get('height') is not None:
                    formats['video'].append({
                        'id': f['format_id'],
                        'height': f['height'],
                        'ext': f['ext']
                    })
                elif f.get('acodec') != 'none' and f.get('abr') is not None:
                    formats['audio'].append({
                        'id': f['format_id'],
                        'bitrate': f.get('abr', 0),
                        'ext': f['ext']
                    })

            formats['video'] = sorted(
                [f for f in formats['video'] if f['ext'] in ['mp4', 'webm'] and f.get('height') is not None],
                key=lambda x: x['height'],
                reverse=True
            )[:5]

            formats['audio'] = sorted(
                [f for f in formats['audio'] if f.get('bitrate') is not None],
                key=lambda x: x['bitrate'],
                reverse=True
            )[:3]

            return jsonify({
                'title': sanitize_filename(info['title']),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration', 0),
                'formats': formats
            })
    except Exception as e:
        app.logger.error(f"Error during get_info: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def download():
    if not request.is_json:
        return jsonify({'error': 'Expected JSON input'}), 400
    try:
        data = request.json
        url = data['url']
        format_type = data.get('format_type', 'video')
        format_id = data.get('format_id')

        if not is_youtube_url(url):
            return jsonify({'error': 'Invalid YouTube URL'}), 400

        job_id = str(uuid.uuid4())
        with lock:
            download_queue.append({
                'id': job_id,
                'title': sanitize_filename(data.get('title', 'video')),
                'status': 'queued',
                'progress': 0,
                'format_type': format_type,
                'format_id': format_id,
                'url': url,
                'thumbnail': data.get('thumbnail', ''),
                'duration': data.get('duration', 0),
                'start_time': time.time()
            })

        return jsonify({'job_id': job_id})
    except Exception as e:
        app.logger.error(f"Download error: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def status():
    with lock:
        return jsonify({'queue': download_queue, 'current': current_download})

@app.route('/downloads')
def list_downloads():
    with lock:
        return jsonify({'history': download_history, 'queue': download_queue, 'current': current_download})

@app.route('/delete_file/<filename>', methods=['DELETE'])
def delete_file(filename):
    try:
        safe_name = sanitize_filename(unquote(filename))
        file_path = os.path.join(app.config['DOWNLOAD_FOLDER'], safe_name)

        if os.path.exists(file_path):
            os.remove(file_path)
            with lock:
                download_history[:] = [f for f in download_history if f['filename'] != safe_name]
            return jsonify({'success': True})
        return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/generate_phone_link/<filename>')
def generate_phone_link(filename):
    safe_name = sanitize_filename(unquote(filename))
    return jsonify({
        'url': f'http://{request.host}/download_file/{safe_name}',
        'qr': f'http://{request.host}/download_file/{safe_name}'
    })

@app.route('/download_file/<filename>')
def download_file(filename):
    try:
        safe_name = sanitize_filename(unquote(filename))
        return send_file(
            os.path.join(app.config['DOWNLOAD_FOLDER'], safe_name),
            as_attachment=True,
            mimetype='application/octet-stream',
            download_name=safe_name,
            conditional=True
        )
    except Exception as e:
        return str(e), 404

def download_worker():
    global current_download
    with lock:
        for file_path in glob.glob(os.path.join(app.config['DOWNLOAD_FOLDER'], '*')):
            if os.path.isfile(file_path):
                filename = os.path.basename(file_path)
                download_history.append({
                    'id': str(uuid.uuid4()),
                    'title': os.path.splitext(filename)[0],
                    'filename': filename,
                    'format': 'mp3' if filename.endswith('.mp3') else 'mp4',
                    'timestamp': datetime.fromtimestamp(os.path.getctime(file_path)).isoformat(),
                    'path': file_path
                })

    while True:
        try:
            with lock:
                if download_queue and not current_download:
                    current_download = download_queue.pop(0)
                    current_download['status'] = 'downloading'
                    current_download['path'] = None

            if current_download:
                try:
                    ydl_opts = {
                        'format': current_download['format_id'],
                        'outtmpl': os.path.join(app.config['DOWNLOAD_FOLDER'], '%(title)s.%(ext)s')
                    }

                    if current_download['format_type'] == 'audio':
                        ydl_opts['postprocessors'] = [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }]

                    def progress_hook(d):
                        if d['status'] == 'downloading':
                            elapsed = time.time() - current_download['start_time']
                            if elapsed > 600:
                                raise Exception("Download timed out")
                            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
                            with lock:
                                current_download['progress'] = min(d['downloaded_bytes'] / total, 0.99)

                    ydl_opts['progress_hooks'] = [progress_hook]

                    with YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(current_download['url'], download=True)
                        final_filename = ydl.prepare_filename(info)
                        if current_download['format_type'] == 'audio':
                            final_filename = final_filename.rsplit('.', 1)[0] + '.mp3'

                    with lock:
                        current_download['status'] = 'complete'
                        current_download['progress'] = 1.0
                        download_history.append({
                            'id': str(uuid.uuid4()),
                            'title': current_download['title'],
                            'filename': os.path.basename(final_filename),
                            'format': current_download['format_type'],
                            'timestamp': datetime.now().isoformat(),
                            'path': final_filename
                        })
                except Exception as e:
                    with lock:
                        current_download['status'] = f'error: {str(e)}'
                finally:
                    with lock:
                        current_download = None

            time.sleep(1)
        except Exception as e:
            app.logger.error(f"Worker crash: {str(e)}", exc_info=True)
            time.sleep(5)

if __name__ == '__main__':
    threading.Thread(target=download_worker, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)