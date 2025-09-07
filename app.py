#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask Camera Monitor with Dual Stream and PTZ Control
Flask-based dual camera monitoring system with real-time streaming and PTZ control
"""

import os
import configparser
import cv2
import time
import threading
from flask import Flask, render_template, Response, request, jsonify
from onvif import ONVIFCamera
from zeep.exceptions import Fault
import logging

# Setup logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('camera_debug.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

class CameraController:
    def __init__(self, config_file='config.ini'):
        logger.info("=== Camera Controller Initialization ===")
        
        self.config = configparser.ConfigParser()
        self.config.read(config_file, encoding='utf-8')
        
        # Read settings from config file
        self.camera_ip = self.config.get('camera', 'ip', fallback='192.168.55.98')
        self.camera_port = self.config.getint('camera', 'onvif_port', fallback=2020)
        self.username = self.config.get('camera', 'username', fallback='steel540')
        self.password = self.config.get('camera', 'password', fallback='11111')
        
        # Dual camera stream settings
        self.rtsp_stream2 = self.config.get('camera', 'rtsp_stream2', 
                                           fallback=f'rtsp://{self.username}:{self.password}@{self.camera_ip}:554/stream2')
        self.rtsp_stream6 = self.config.get('camera', 'rtsp_stream6', 
                                           fallback=f'rtsp://{self.username}:{self.password}@{self.camera_ip}:554/stream6')
        
        # PTZ settings
        self.ptz_speed = self.config.getfloat('ptz', 'speed', fallback=0.4)
        self.ptz_duration = self.config.getfloat('ptz', 'duration', fallback=0.5)
        
        logger.info(f"Configuration loaded:")
        logger.info(f"  Camera IP: {self.camera_ip}")
        logger.info(f"  ONVIF Port: {self.camera_port}")
        logger.info(f"  Username: {self.username}")
        logger.info(f"  PTZ Speed: {self.ptz_speed}")
        logger.info(f"  PTZ Duration: {self.ptz_duration}")
        logger.info(f"  Stream2 URL: {self.rtsp_stream2}")
        logger.info(f"  Stream6 URL: {self.rtsp_stream6}")
        
        # ONVIF connection
        self.cam = None
        self.ptz = None
        self.profile = None
        self.connect_onvif()
        
        # PTZ video stream (stream6)
        self.cap_ptz = None
        self.frame_ptz = None
        self.last_frame_time_ptz = 0
        self.streaming_ptz = False
        
        # Fixed video stream (stream2)
        self.cap_fixed = None
        self.frame_fixed = None
        self.last_frame_time_fixed = 0
        self.streaming_fixed = False
        
        logger.info("=== Camera Controller Initialization Complete ===")
        
    def connect_onvif(self):
        """Connect to ONVIF service"""
        logger.info("=== ONVIF Connection Start ===")
        logger.info(f"Camera IP: {self.camera_ip}")
        logger.info(f"ONVIF Port: {self.camera_port}")
        logger.info(f"Username: {self.username}")
        
        try:
            logger.info("Establishing ONVIF camera connection...")
            self.cam = ONVIFCamera(self.camera_ip, self.camera_port, 
                                 self.username, self.password, no_cache=True)
            
            logger.info("Setting transport parameters...")
            try:
                self.cam.transport.ws_client.transport.session.verify = True
                self.cam.transport.ws_client.transport.session.trust_env = True
                self.cam.set_datetime_offset()
                logger.info("Transport parameters configured")
            except Exception as transport_e:
                logger.warning(f"Transport parameter configuration failed (can be ignored): {transport_e}")
            
            logger.info("Creating media service...")
            media = self.cam.create_media_service()
            logger.info("Creating PTZ service...")
            self.ptz = self.cam.create_ptz_service()
            
            logger.info("Getting media profiles...")
            profiles = media.GetProfiles()
            if not profiles:
                raise RuntimeError("No media profiles found")
            
            logger.info(f"Found {len(profiles)} profiles:")
            for i, p in enumerate(profiles):
                has_ptz = bool(getattr(p, 'PTZConfiguration', None))
                logger.info(f"  Profile {i}: token={p.token}, PTZ support={has_ptz}")
            
            # Prefer profile with PTZ configuration
            self.profile = None
            for p in profiles:
                if getattr(p, 'PTZConfiguration', None):
                    self.profile = p
                    logger.info(f"Selected PTZ-enabled profile: {p.token}")
                    break
            
            if self.profile is None:
                self.profile = profiles[0]
                logger.warning(f"No PTZ configuration found, using default profile: {self.profile.token}")
            
            # Test PTZ functionality
            logger.info("Testing PTZ functionality...")
            try:
                status = self.ptz.GetStatus({'ProfileToken': self.profile.token})
                logger.info(f"PTZ status test successful")
            except Exception as ptz_test_e:
                logger.warning(f"PTZ functionality test failed: {ptz_test_e}")
            
            logger.info(f"ONVIF connection successful: {self.camera_ip}:{self.camera_port}")
            logger.info("=== ONVIF Connection Complete ===")
            
        except Exception as e:
            logger.error(f"ONVIF connection failed: {e}")
            import traceback
            logger.error(f"Full error traceback: {traceback.format_exc()}")
            self.cam = None
            self.ptz = None
            self.profile = None
    
    def start_stream_ptz(self):
        """Start PTZ video stream"""
        if self.streaming_ptz:
            return
            
        self.streaming_ptz = True
        thread = threading.Thread(target=self._stream_worker_ptz)
        thread.daemon = True
        thread.start()
        logger.info("PTZ stream thread started")
    
    def start_stream_fixed(self):
        """Start fixed video stream"""
        if self.streaming_fixed:
            return
            
        self.streaming_fixed = True
        thread = threading.Thread(target=self._stream_worker_fixed)
        thread.daemon = True
        thread.start()
        logger.info("Fixed stream thread started")
        
    def _stream_worker_ptz(self):
        """PTZ video stream worker thread"""
        retry_count = 0
        max_retries = 5
        
        while self.streaming_ptz:
            try:
                logger.info(f"Attempting to connect PTZ video stream: {self.rtsp_stream6}")
                self.cap_ptz = cv2.VideoCapture(self.rtsp_stream6)
                
                # Set stream parameters
                self.cap_ptz.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cap_ptz.set(cv2.CAP_PROP_FPS, 15)
                
                # Test if reading is possible
                ret, test_frame = self.cap_ptz.read()
                if not ret:
                    logger.error("Cannot read PTZ test frame, reconnecting...")
                    if self.cap_ptz:
                        self.cap_ptz.release()
                    time.sleep(2)
                    continue
                
                logger.info(f"PTZ video stream connected successfully")
                retry_count = 0
                
                # Main reading loop
                consecutive_fails = 0
                while self.streaming_ptz:
                    ret, frame = self.cap_ptz.read()
                    if ret:
                        self.frame_ptz = frame
                        self.last_frame_time_ptz = time.time()
                        consecutive_fails = 0
                    else:
                        consecutive_fails += 1
                        logger.warning(f"PTZ video frame read failed ({consecutive_fails}/10)")
                        
                        if consecutive_fails >= 10:
                            logger.error("Too many consecutive PTZ read failures, reconnecting stream...")
                            break
                            
                        time.sleep(0.1)
                        
            except Exception as e:
                retry_count += 1
                logger.error(f"PTZ video stream error (retry {retry_count}/{max_retries}): {e}")
                
                if retry_count >= max_retries:
                    logger.error("PTZ reached maximum retry attempts, stopping stream")
                    break
                    
                time.sleep(min(retry_count * 2, 10))
                
            finally:
                if self.cap_ptz:
                    self.cap_ptz.release()
                    self.cap_ptz = None
                    
        logger.info("PTZ video stream worker thread ended")
    
    def _stream_worker_fixed(self):
        """Fixed video stream worker thread"""
        retry_count = 0
        max_retries = 5
        
        while self.streaming_fixed:
            try:
                logger.info(f"Attempting to connect fixed video stream: {self.rtsp_stream2}")
                self.cap_fixed = cv2.VideoCapture(self.rtsp_stream2)
                
                # Set stream parameters
                self.cap_fixed.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cap_fixed.set(cv2.CAP_PROP_FPS, 15)
                
                # Test if reading is possible
                ret, test_frame = self.cap_fixed.read()
                if not ret:
                    logger.error("Cannot read fixed test frame, reconnecting...")
                    if self.cap_fixed:
                        self.cap_fixed.release()
                    time.sleep(2)
                    continue
                
                logger.info(f"Fixed video stream connected successfully")
                retry_count = 0
                
                # Main reading loop
                consecutive_fails = 0
                while self.streaming_fixed:
                    ret, frame = self.cap_fixed.read()
                    if ret:
                        self.frame_fixed = frame
                        self.last_frame_time_fixed = time.time()
                        consecutive_fails = 0
                    else:
                        consecutive_fails += 1
                        logger.warning(f"Fixed video frame read failed ({consecutive_fails}/10)")
                        
                        if consecutive_fails >= 10:
                            logger.error("Too many consecutive fixed read failures, reconnecting stream...")
                            break
                            
                        time.sleep(0.1)
                        
            except Exception as e:
                retry_count += 1
                logger.error(f"Fixed video stream error (retry {retry_count}/{max_retries}): {e}")
                
                if retry_count >= max_retries:
                    logger.error("Fixed reached maximum retry attempts, stopping stream")
                    break
                    
                time.sleep(min(retry_count * 2, 10))
                
            finally:
                if self.cap_fixed:
                    self.cap_fixed.release()
                    self.cap_fixed = None
                    
        logger.info("Fixed video stream worker thread ended")
    
    def restart_stream_ptz(self):
        """Restart PTZ video stream"""
        logger.info("=== Restarting PTZ Video Stream ===")
        
        old_streaming = self.streaming_ptz
        self.streaming_ptz = False
        
        time.sleep(1)
        
        if self.cap_ptz:
            self.cap_ptz.release()
            self.cap_ptz = None
            
        if old_streaming:
            self.start_stream_ptz()
            logger.info("PTZ video stream restart complete")
    
    def restart_stream_fixed(self):
        """Restart fixed video stream"""
        logger.info("=== Restarting Fixed Video Stream ===")
        
        old_streaming = self.streaming_fixed
        self.streaming_fixed = False
        
        time.sleep(1)
        
        if self.cap_fixed:
            self.cap_fixed.release()
            self.cap_fixed = None
            
        if old_streaming:
            self.start_stream_fixed()
            logger.info("Fixed video stream restart complete")
    
    def get_frame_ptz(self):
        """Get current PTZ frame"""
        if self.frame_ptz is not None:
            ret, buffer = cv2.imencode('.jpg', self.frame_ptz, 
                                     [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                return buffer.tobytes()
        return None
    
    def get_frame_fixed(self):
        """Get current fixed frame"""
        if self.frame_fixed is not None:
            ret, buffer = cv2.imencode('.jpg', self.frame_fixed, 
                                     [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                return buffer.tobytes()
        return None
    
    def stop_ptz(self):
        """Stop PTZ movement"""
        logger.info("=== PTZ Stop Begin ===")
        
        if not self.ptz or not self.profile:
            logger.error("PTZ service or Profile not initialized, cannot stop")
            return False
            
        try:
            logger.info(f"Using Profile Token to stop: {self.profile.token}")
            self.ptz.Stop({'ProfileToken': self.profile.token})
            logger.info("PTZ stop command sent")
            logger.info("=== PTZ Stop Complete ===")
            return True
        except Fault as e:
            logger.error(f"PTZ stop Fault error: {e}")
            return False
        except Exception as e:
            logger.error(f"PTZ stop exception: {e}")
            return False
    
    def move_ptz(self, direction):
        """PTZ movement control"""
        logger.info(f"=== PTZ Movement Begin ===")
        logger.info(f"Direction: {direction}")
        logger.info(f"Speed: {self.ptz_speed}")
        logger.info(f"Duration: {self.ptz_duration}")
        
        if not self.ptz or not self.profile:
            logger.error("PTZ service or Profile not initialized")
            return False
        
        logger.info(f"Using Profile Token: {self.profile.token}")
            
        try:
            # Check PTZ support status
            try:
                status = self.ptz.GetStatus({'ProfileToken': self.profile.token})
                logger.info(f"PTZ status check successful")
            except Exception as status_e:
                logger.warning(f"PTZ status check failed: {status_e}")
            
            # Build movement request
            req = self.ptz.create_type('ContinuousMove')
            req.ProfileToken = self.profile.token
            req.Velocity = {}
            
            x = y = z = 0.0
            speed = max(0.05, min(1.0, float(self.ptz_speed)))
            
            logger.info(f"Adjusted speed: {speed}")
            
            if direction == 'up':
                y = +speed
            elif direction == 'down':
                y = -speed
            elif direction == 'left':
                x = -speed
            elif direction == 'right':
                x = +speed
            elif direction == 'upleft':
                x, y = -speed, +speed
            elif direction == 'upright':
                x, y = +speed, +speed
            elif direction == 'downleft':
                x, y = -speed, -speed
            elif direction == 'downright':
                x, y = +speed, -speed
            elif direction == 'zoom_in':
                z = +speed
            elif direction == 'zoom_out':
                z = -speed
            
            logger.info(f"Calculated movement vector: x={x}, y={y}, z={z}")
            
            # Set movement vector
            if x != 0.0 or y != 0.0:
                req.Velocity['PanTilt'] = {'x': float(x), 'y': float(y)}
                logger.info(f"Set PanTilt vector: {req.Velocity['PanTilt']}")
            
            if z != 0.0:
                req.Velocity['Zoom'] = {'x': float(z)}
                logger.info(f"Set Zoom vector: {req.Velocity['Zoom']}")
            
            # Execute movement
            logger.info("Starting PTZ movement execution...")
            self.ptz.ContinuousMove(req)
            logger.info("PTZ movement command sent")
            
            # Wait then auto-stop
            logger.info(f"Waiting {self.ptz_duration} seconds before auto-stop...")
            time.sleep(max(0.05, self.ptz_duration))
            
            # Stop movement
            logger.info("Sending stop command...")
            self.stop_ptz()
            logger.info("=== PTZ Movement Complete ===")
            
            return True
            
        except Fault as e:
            logger.error(f"ONVIF Fault error: {e}")
            return False
        except Exception as e:
            logger.error(f"PTZ movement exception: {e}")
            import traceback
            logger.error(f"Full error traceback: {traceback.format_exc()}")
            return False

# Global camera controller
camera_controller = CameraController()

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    """PTZ video stream endpoint"""
    def generate():
        camera_controller.start_stream_ptz()
        while True:
            frame = camera_controller.get_frame_ptz()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                time.sleep(0.1)
    
    return Response(generate(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video_feed_fixed')
def video_feed_fixed():
    """Fixed video stream endpoint"""
    def generate():
        camera_controller.start_stream_fixed()
        while True:
            frame = camera_controller.get_frame_fixed()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                time.sleep(0.1)
    
    return Response(generate(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/ptz_control', methods=['POST'])
def ptz_control():
    """PTZ control API"""
    logger.info("=== PTZ Control Request Received ===")
    
    try:
        data = request.get_json()
        direction = data.get('direction')
        
        logger.info(f"Request direction: {direction}")
        
        if direction == 'stop':
            logger.info("Executing stop command")
            success = camera_controller.stop_ptz()
        else:
            logger.info(f"Executing movement command: {direction}")
            success = camera_controller.move_ptz(direction)
        
        logger.info(f"PTZ control result: {'Success' if success else 'Failed'}")
        logger.info("=== PTZ Control Request Complete ===")
        
        return jsonify({'success': success, 'direction': direction})
        
    except Exception as e:
        logger.error(f"PTZ control API error: {e}")
        import traceback
        logger.error(f"Full error traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/restart_stream', methods=['POST'])
def restart_stream():
    """Restart video stream API"""
    try:
        data = request.get_json()
        stream_type = data.get('stream', 'all')
        
        if stream_type == 'ptz':
            camera_controller.restart_stream_ptz()
            message = 'PTZ video stream restarted'
        elif stream_type == 'fixed':
            camera_controller.restart_stream_fixed()
            message = 'Fixed video stream restarted'
        else:  # 'all'
            camera_controller.restart_stream_ptz()
            camera_controller.restart_stream_fixed()
            message = 'All video streams restarted'
            
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        logger.error(f"Stream restart failed: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/status')
def status():
    """System status"""
    onvif_status = camera_controller.cam is not None
    ptz_stream_status = camera_controller.frame_ptz is not None
    fixed_stream_status = camera_controller.frame_fixed is not None
    
    return jsonify({
        'onvif_connected': onvif_status,
        'ptz_stream_active': ptz_stream_status,
        'fixed_stream_active': fixed_stream_status,
        'last_frame_time_ptz': camera_controller.last_frame_time_ptz,
        'last_frame_time_fixed': camera_controller.last_frame_time_fixed
    })

if __name__ == '__main__':
    logger.info("=== Application Startup ===")
    
    # Create config file if it doesn't exist
    if not os.path.exists('config.ini'):
        logger.info("Creating default configuration file...")
        config = configparser.ConfigParser()
        config['camera'] = {
            'ip': '192.168.55.98',
            'onvif_port': '2020',
            'username': 'steel540',
            'password': '12345678',
            'rtsp_stream2': 'rtsp://steel540:12345678@192.168.55.98:554/stream2',
            'rtsp_stream6': 'rtsp://steel540:12345678@192.168.55.98:554/stream6'
        }
        config['ptz'] = {
            'speed': '0.4',
            'duration': '0.5'
        }
        config['server'] = {
            'host': '0.0.0.0',
            'port': '5000',
            'debug': 'false'
        }
        
        with open('config.ini', 'w', encoding='utf-8') as f:
            config.write(f)
        logger.info("Default configuration file config.ini created")
    
    # Read server configuration
    config = configparser.ConfigParser()
    config.read('config.ini', encoding='utf-8')
    
    host = config.get('server', 'host', fallback='0.0.0.0')
    port = config.getint('server', 'port', fallback=5000)
    debug = config.getboolean('server', 'debug', fallback=False)
    
    logger.info(f"Server configuration: host={host}, port={port}, debug={debug}")
    logger.info(f"Server starting: http://{host}:{port}")
    logger.info("Debug logs will be saved to camera_debug.log")
    logger.info("=== Application Startup Complete ===")
    
    app.run(host=host, port=port, debug=debug, threaded=True)
