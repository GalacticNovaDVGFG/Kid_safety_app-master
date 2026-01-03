#!/usr/bin/env python3
"""Simple Raspberry Pi companion service
- Serves MJPEG stream at /camera/stream
- Provides /camera/start and /camera/stop endpoints
- Monitors buttons and POSTs events to SERVER_URL

This is an example. Adjust pins, security, and camera code for your hardware.
"""

import os
import time
import threading
from flask import Flask, Response, jsonify, request
import requests
import cv2
from dotenv import load_dotenv

try:
    from gpiozero import Button
except Exception:
    Button = None

load_dotenv()

SERVER_URL = os.getenv('SERVER_URL')
DEVICE_ID = os.getenv('DEVICE_ID', 'raspi')
API_KEY = os.getenv('API_KEY', '')
HELP_PIN = int(os.getenv('HELP_BUTTON_PIN', '17'))
POWER_PIN = int(os.getenv('POWER_BUTTON_PIN', '27'))
CAM_INDEX = int(os.getenv('CAMERA_INDEX', '0'))
STREAM_PORT = int(os.getenv('STREAM_PORT', '8000'))

app = Flask(__name__)

frame = None

# Camera manager: supports OpenCV (USB webcams) and optional picamera2 (official Pi camera module)
class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self._stop = threading.Event()
        self.cam_index = CAM_INDEX
        self.backend = os.getenv('CAMERA_BACKEND', 'auto').lower()
        self.picamera_available = False
        self.PC2 = None
        if self.backend in ('auto', 'picamera2'):
            try:
                # Picamera2 is Raspberry Pi specific; allow import errors on non-Pi systems
                from picamera2 import Picamera2  # type: ignore[import]
                self.PC2 = Picamera2
                self.picamera_available = True
            except Exception:
                self.picamera_available = False

    def start(self):
        if self.running:
            return
        self._stop.clear()
        if self.backend == 'opencv' or (self.backend == 'auto' and not self.picamera_available):
            t = threading.Thread(target=self._opencv_loop, daemon=True)
        else:
            # prefer picamera2 when available
            if self.picamera_available:
                t = threading.Thread(target=self._picamera2_loop, daemon=True)
            else:
                t = threading.Thread(target=self._opencv_loop, daemon=True)
        t.start()

    def stop(self):
        self._stop.set()

    def is_running(self):
        return self.running

    def _opencv_loop(self):
        global frame
        cap = cv2.VideoCapture(self.cam_index)
        if not cap.isOpened():
            print('Camera could not be opened (OpenCV)')
            self.running = False
            return
        self.running = True
        print('OpenCV camera started')
        while not self._stop.is_set():
            ret, img = cap.read()
            if not ret:
                time.sleep(0.1)
                continue
            # encode as JPEG
            ret, jpeg = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret:
                frame = jpeg.tobytes()
            time.sleep(0.03)  # ~30fps throttle
        cap.release()
        frame = None
        self.running = False
        print('OpenCV camera stopped')

    def _picamera2_loop(self):
        global frame
        try:
            picam = self.PC2()
            # use preview configuration for smaller latency
            config = picam.create_preview_configuration({'main': {'size': (640, 480)}})
            picam.configure(config)
            picam.start()
        except Exception as e:
            print('Failed to start Picamera2:', e)
            self.running = False
            return
        self.running = True
        print('Picamera2 camera started')
        while not self._stop.is_set():
            try:
                img = picam.capture_array()
                # Picamera2 returns RGB; convert to BGR for OpenCV encoding
                img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                ret, jpeg = cv2.imencode('.jpg', img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ret:
                    frame = jpeg.tobytes()
            except Exception as e:
                print('Picamera2 capture failed', e)
            time.sleep(0.03)
        try:
            picam.stop()
        except Exception:
            pass
        frame = None
        self.running = False
        print('Picamera2 camera stopped')


camera_mgr = CameraManager()


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        'device_id': DEVICE_ID,
        'camera_running': camera_mgr.is_running(),
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200


@app.route('/camera/start', methods=['POST'])
def camera_start():
    if camera_mgr.is_running():
        return jsonify({'status': 'already running'})
    camera_mgr.start()
    return jsonify({'status': 'started'})


@app.route('/camera/stop', methods=['POST'])
def camera_stop():
    if not camera_mgr.is_running():
        return jsonify({'status': 'already stopped'})
    camera_mgr.stop()
    return jsonify({'status': 'stopping'})


def generate_mjpeg():
    global frame
    while True:
        if frame is None:
            time.sleep(0.05)
            continue
        chunk = (b'--frame\r\n'
                 b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        yield chunk
        time.sleep(0.03)


@app.route('/camera/stream')
def camera_stream():
    if not camera_mgr.is_running():
        return jsonify({'error': 'camera not running'}), 503
    return Response(generate_mjpeg(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/help', methods=['POST'])
def help_endpoint():
    data = {
        'device_id': DEVICE_ID,
        'event': 'help_pressed',
        'timestamp': int(time.time()),
    }
    headers = {'Authorization': f'Bearer {API_KEY}'} if API_KEY else {}
    try:
        if SERVER_URL:
            requests.post(SERVER_URL, json=data, headers=headers, timeout=5)
        else:
            print('No SERVER_URL configured, skipping POST', data)
    except Exception as e:
        print('Failed to post help event', e)
    return jsonify({'status': 'sent'})


def button_worker():
    # Allow disabling button monitoring on non-Pi/dev systems
    if os.getenv('DISABLE_BUTTONS', '0') == '1':
        print('DISABLE_BUTTONS=1, skipping button monitoring')
        return

    if Button is None:
        print('gpiozero not available, skipping button monitoring')
        return

    def on_help():
        print('Help button pressed')
        # send local event
        try:
            requests.post(f'http://localhost:{STREAM_PORT}/help', timeout=2)
        except Exception:
            pass

        # send remote event
        data = {'device_id': DEVICE_ID, 'event': 'help_pressed', 'timestamp': int(time.time())}
        headers = {'Authorization': f'Bearer {API_KEY}'} if API_KEY else {}
        if SERVER_URL:
            try:
                requests.post(SERVER_URL, json=data, headers=headers, timeout=5)
            except Exception as e:
                print('Failed to post to SERVER_URL', e)

    def on_power():
        print('Power toggle pressed - toggling camera')
        # Toggle camera by calling local endpoints first; fall back to direct control
        try:
            if camera_mgr.is_running():
                requests.post(f'http://localhost:{STREAM_PORT}/camera/stop', timeout=2)
            else:
                requests.post(f'http://localhost:{STREAM_PORT}/camera/start', timeout=2)
        except Exception:
            # If local requests fail, call manager directly as a best-effort fallback
            try:
                if camera_mgr.is_running():
                    camera_mgr.stop()
                else:
                    camera_mgr.start()
            except Exception:
                pass

    # Try to initialize the buttons. On non-Raspberry Pi systems this can raise a
    # BadPinFactory (or other) exception; handle that gracefully and continue.
    try:
        help_btn = Button(HELP_PIN)
        help_btn.when_pressed = on_help

        power_btn = Button(POWER_PIN)
        power_btn.when_pressed = on_power
    except Exception as e:
        print('GPIO not available or unsupported on this platform, skipping button monitoring:', e)
        return


if __name__ == '__main__':
    # Start button worker in background
    t = threading.Thread(target=button_worker, daemon=True)
    t.start()

    # Optionally start camera automatically
    auto_start = os.getenv('AUTO_START_CAMERA', '1')
    if auto_start == '1':
        camera_mgr.start()

    print(f'Starting Flask on 0.0.0.0:{STREAM_PORT}')
    app.run(host='0.0.0.0', port=STREAM_PORT)
