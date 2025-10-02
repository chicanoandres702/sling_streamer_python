# app.py
#
# Dual-purpose Sling TV Streamer with Audio Support.
#
# USAGE:
#   - To run in Web Server mode:
#     python app.py
#
#   - To run in Command-Line Interface (CLI) mode:
#     python app.py --list-channels
#     python app.py --channel <CHANNEL_ID>
#     python app.py --channel <CHANNEL_ID> --output stream.mp4

import os
import sys
import json
import time
import queue
import base64
import tempfile
import threading
import subprocess
import asyncio
import socket
import logging
import argparse
from urllib.parse import urljoin

import requests
import jwt
import m3u8
import websockets
from flask import Flask, jsonify, render_template
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# ==============================================================================
# --- 1. CENTRALIZED CONFIGURATION ---
# ==============================================================================
WVD_FILE = 'WVD.wvd'
HTTP_HOST = '0.0.0.0'
HTTP_PORT = 8000
WEBSOCKET_HOST = '0.0.0.0'
WEBSOCKET_PORT = 8765

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
SLING_JWT = os.environ.get('SLING_JWT', "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2NvdW50U3RhdHVzIjoiYWN0aXZlIiwiZGV2IjoiNjlmYWFjOTctYmMwZi00Y2RhLTllNGYtOTZkOTk4YzQ4Nzg3IiwiaWF0IjoxNzU5MzUxOTg0LCJpc3MiOiJDTVciLCJwbGF0IjoiYnJvd3NlciIsInByb2QiOiJzbGluZyIsInByb2YiOiIyODgwYzg0NC1mMmQ1LTExZTktODMwZi0wZTIwYTUxZDhlN2MiLCJwcm9maWxlVHlwZSI6IkFkbWluIiwic3ViIjoiMjg4MGM4NDQtZjJkNS0xMWU5LTgzMGYtMGUyMGE1MWQ4ZTdjIn0.zGhss5iouL7-OV30Qf_cZlj-AoUGfqigmXBrAIp5qAk")

# ==============================================================================
# --- 2. CORE STREAMING LOGIC ---
# ==============================================================================
class CoreConfig:
    def __init__(self, wvd_path):
        self.WVD_PATH = wvd_path
        self.SLING_STREAM_AUTH_URL = 'https://p-streamauth.movetv.com/stream/auth'
        self.SLING_WIDEVINE_PROXY_URL = 'https://p-drmwv.movetv.com/widevine/proxy'
        self.SLING_CHANNELS_URL = 'https://p-cmwnext.movetv.com/pres/grid_guide_a_z'

class KeyManager:
    def __init__(self):
        self._is_unix = os.name == 'posix'
        self._key_source_path = None
        self._temp_file_handle = None
    
    def setup(self):
        if self._is_unix:
            self._key_source_path = os.path.join(tempfile.gettempdir(), f"ffmpeg_keys_{os.getpid()}")
            os.mkfifo(self._key_source_path)
        else:
            self._temp_file_handle = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".txt")
            self._key_source_path = self._temp_file_handle.name
    
    def get_key_source_path(self):
        return self._key_source_path
    
    def provide_keys(self, keys):
        key_data = "\n".join([f"{kid}:{cek}" for kid, cek in keys.items()])
        logging.info(f"Writing {len(keys)} decryption key(s) to {self._key_source_path}")
        try:
            if self._is_unix:
                with open(self._key_source_path, 'w') as pipe:
                    pipe.write(key_data)
                    pipe.flush()
            else:
                self._temp_file_handle.write(key_data)
                self._temp_file_handle.flush()
        except Exception as e:
            logging.error(f"KeyManager Error: {e}")
    
    def cleanup(self):
        if not self._is_unix and self._temp_file_handle:
            try:
                if not self._temp_file_handle.closed:
                    self._temp_file_handle.close()
            except Exception:
                pass
            self._temp_file_handle = None
        if self._key_source_path and os.path.exists(self._key_source_path):
            try:
                os.remove(self._key_source_path)
            except OSError as e:
                logging.warning(f"KeyManager Cleanup Warning: {e}")

class SlingClient:
    def __init__(self, user_jwt, config):
        self.config=config; self._user_jwt=user_jwt; self._subscriber_id=jwt.decode(self._user_jwt,options={"verify_signature":False})['prof']
    
    def get_channel_list(self):
        headers = { 'authorization': f'Bearer {self._user_jwt}', 'client-config': 'rn-client-config', 'client-version': '4.32.20', 'content-type': 'application/json; charset=UTF-8', 'device-model': 'Chrome', 'dma': '501', 'features': 'use_ui_4=true,inplace_reno_invalidation=true,gzip_response=true,enable_extended_expiry=true,enable_home_channels=true,enable_iap=true,enable_trash_franchise_iview=false,browse-by-service-ribbon=true,subpack-hub-view=true,entitled_streaming_hub=false,add-premium-channels=false,enable_home_sports_scores=false,enable-basepack-ribbon=true', 'page_size': 'large', 'player-version': '7.6.2', 'response-config': 'ar_browser_1_1', 'sling-interaction-id': '596bd797-90f1-440f-bc02-e6f588dae8f6', 'timezone': 'America/Los_Angeles' }
        try:
            response = requests.get(self.config.SLING_CHANNELS_URL, headers=headers, timeout=15).json()
            channels = []
            if 'special_ribbons' in response and response['special_ribbons']:
                tiles = response['special_ribbons'][0].get('tiles', [])
                for tile in tiles: channels.append({'title': tile.get('title'), 'id': tile.get('href', '').split('/')[-1]})
            return channels
        except (requests.RequestException, IndexError, KeyError) as e: logging.error(f"Could not fetch/parse channel list: {e}"); return []
    
    def authenticate_stream(self,channel_id):
        headers={'authorization':f'Bearer {self._user_jwt}','Content-Type':'application/json'}
        payload={'subscriber_id':self._subscriber_id,'drm':'widevine','qvt':f'https://cbd46b77.cdn.cms.movetv.com/playermetadata/sling/v1/api/channels/{channel_id}/current/schedule.qvt','os_version':'10','device_name':'browser','os_name':'Windows','brand':'sling','account_status':'active','advertiser_id':None,'support_mode':'false','ssai_vod':'true','ssai_dvr':'true',}
        try:
            response=requests.post(self.config.SLING_STREAM_AUTH_URL,headers=headers,json=payload,timeout=20); response.raise_for_status(); data=response.json()
            stream_manifest_url=data.get('ssai_manifest','')
            key_manifest_url=data.get('m3u8_url')
            temp_jwt=data.get('jwt')
            if not stream_manifest_url or not temp_jwt or not key_manifest_url: raise ValueError("Stream auth response missing data.")
            return stream_manifest_url, key_manifest_url, temp_jwt
        except(requests.RequestException,ValueError) as e: logging.error(f"Error authenticating stream: {e}"); return None, None, None
    
    def get_pssh_from_hls(self, hls_url):
        try:
            response = requests.get(hls_url, timeout=10)
            response.raise_for_status()
            m3u8_obj = m3u8.loads(content=response.text, uri=response.url)
            for key in m3u8_obj.session_keys:
                if key and key.uri: return key.uri.split(',')[-1]
            raise ValueError("PSSH data not found in HLS manifest session keys.")
        except Exception as e: logging.error(f"Error getting PSSH from HLS: {e}"); return None

    def derive_audio_url(self, video_url):
        """
        Derives the audio URL from the master video manifest.
        It tries to find an associated audio-only stream from the manifest's media tags.
        If it fails, it falls back to the old string replacement method.
        """
        try:
            master_playlist_content = requests.get(video_url, timeout=10).text
            master_playlist = m3u8.loads(master_playlist_content, uri=video_url)

            if master_playlist.is_variant:
                # Find the highest bandwidth video stream to identify the active audio group
                best_video_playlist = sorted(master_playlist.playlists, key=lambda p: p.stream_info.bandwidth, reverse=True)[0]
                audio_group_id = best_video_playlist.stream_info.audio
                
                if audio_group_id:
                    # Find all audio media of that group
                    audio_media = [m for m in master_playlist.media if m.type == 'AUDIO' and m.group_id == audio_group_id]
                    if audio_media:
                        # Select the first available audio stream from the group (e.g., default language)
                        # and ensure it has a URI (i.e., it's not multiplexed)
                        for media in audio_media:
                            if media.absolute_uri:
                                logging.info(f"Found audio stream from manifest: {media.absolute_uri}")
                                return media.absolute_uri

        except Exception as e:
            logging.warning(f"Could not parse master manifest to find audio stream: {e}. Falling back to URL derivation.")

        # Fallback to old method if new logic fails or finds no suitable stream
        logging.warning("Falling back to legacy audio URL derivation.")
        audio_url = video_url.replace('/video/', '/audio/')
        audio_url = audio_url.replace('/vid06.m3u8', '/stereo/192.m3u8')
        logging.info(f"Derived audio URL (fallback): {audio_url}")
        return audio_url

class WidevineManager:
    def __init__(self, config):
        self.config = config
        if not os.path.exists(config.WVD_PATH):
            raise FileNotFoundError(f"WVD file not found at '{config.WVD_PATH}'")
        self._device = Device.load(config.WVD_PATH)
    
    def get_decryption_keys(self, pssh_b64, temp_jwt):
        try:
            license_channel_id = jwt.decode(temp_jwt, options={"verify_signature": False})['channel_guid']
            pssh = PSSH(base64.b64decode(pssh_b64))
            cdm = Cdm.from_device(self._device)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh)
            
            headers = {
                'Authorization': f'Bearer {temp_jwt}',
                'Env': 'production',
                'Channel-Id': license_channel_id,
                'Content-Type': 'application/json'
            }
            payload = {"message": list(challenge)}
            
            response = requests.post(self.config.SLING_WIDEVINE_PROXY_URL, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            cdm.parse_license(session_id, response.content)
            
            keys = {}
            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    if isinstance(key.kid, bytes):
                        kid_hex = key.kid.hex()
                    else:
                        kid_hex = str(key.kid)
                    
                    if isinstance(key.key, bytes):
                        key_hex = key.key.hex()
                    else:
                        key_hex = str(key.key)
                    
                    kid_hex = kid_hex.replace('-', '')
                    key_hex = key_hex.replace('-', '')
                    
                    keys[kid_hex] = key_hex
                    logging.info(f"Extracted key: KID={kid_hex[:16]}... KEY={key_hex[:16]}...")
            
            cdm.close(session_id)
            
            if not keys:
                raise ValueError("No content keys were returned.")
            
            return keys
            
        except (requests.RequestException, ValueError, FileNotFoundError) as e:
            logging.error(f"Error during Widevine license acquisition: {e}")
            return {}

class FFmpegProcess:
    """
    Improved FFmpeg process manager with proper synchronization
    and separate key handling for video and audio streams
    """
    def __init__(self, keys_dict=None):
        self._keys_dict = keys_dict or {}
        self.video_process = None
        self.audio_process = None
        self.muxer_process = None
        self.process = None  # For compatibility
        self.stderr_log = []
        self.video_pipe = None
        self.audio_pipe = None
        self._stop_event = threading.Event()
        self._video_ready = threading.Event()
        self._audio_ready = threading.Event()
        self._key_files = []

    def _create_key_file(self):
        """Create a temporary file containing decryption keys"""
        if not self._keys_dict:
            return None
        
        key_data = "\n".join([f"{kid}:{cek}" for kid, cek in self._keys_dict.items()])
        
        # Create a temporary file that will be deleted on close
        fd, path = tempfile.mkstemp(suffix='.txt', text=True)
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(key_data)
                f.flush()
            self._key_files.append(path)
            logging.info(f"Created key file: {path}")
            return path
        except Exception as e:
            logging.error(f"Failed to create key file: {e}")
            os.close(fd)
            return None

    def _get_free_port(self):
        """Get a free TCP port"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _setup_tcp_server(self, name):
        """Create and return a TCP server socket"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]
        logging.info(f"{name} TCP server listening on port {port}")
        return server, port

    def start(self):
        """Start FFmpeg processes with proper synchronization"""
        if os.name != 'nt':
            raise NotImplementedError("Multi-stream FFmpeg is currently Windows-only")

        try:
            # Create key file if we have keys
            key_file = self._create_key_file()
            decryption_args = []
            if key_file:
                # Note: Using decryption_key requires a single key. This setup assumes video/audio use the same key.
                # If they can differ, a more complex setup with per-stream key files would be needed.
                decryption_args = ['-decryption_key', list(self._keys_dict.values())[0]]

            # Setup input sockets
            video_server, video_port = self._setup_tcp_server("Video input")
            audio_server, audio_port = self._setup_tcp_server("Audio input")

            # Setup muxer input ports
            video_mux_port = self._get_free_port()
            audio_mux_port = self._get_free_port()

            # Build FFmpeg commands
            video_cmd = [
                'ffmpeg', '-y',
                *decryption_args,
                '-i', f'tcp://127.0.0.1:{video_port}?timeout=10000000',
                '-c:v', 'copy',
                '-copyts',
                '-f', 'mpegts',
                f'tcp://127.0.0.1:{video_mux_port}?listen=1&timeout=10000000'
            ]
            
            audio_cmd = [
                'ffmpeg', '-y',
                *decryption_args,
                '-i', f'tcp://127.0.0.1:{audio_port}?timeout=10000000',
                '-c:a', 'copy',
                '-copyts',
                '-f', 'mpegts',
                f'tcp://127.0.0.1:{audio_mux_port}?listen=1&timeout=10000000'
            ]
            
            # --- MODIFICATION START ---
            # This muxer command now includes timestamp-based synchronization flags.
            muxer_cmd = [
                'ffmpeg', '-y',
                '-i', f'tcp://127.0.0.1:{video_mux_port}?timeout=10000000',
                '-i', f'tcp://127.0.0.1:{audio_mux_port}?timeout=10000000',
                '-map', '0:v',
                '-map', '1:a',
                '-fflags', '+genpts',
                # Preserve original timestamps and sync audio start to video
                '-copyts',
                '-async', '1',
                '-bsf:a', 'aac_adtstoasc',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-c:a', 'aac', '-b:a', '192k',
                '-f', 'mp4',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-'
            ]
            # --- MODIFICATION END ---

            logging.info(f"Starting video decoder...")
            self.video_process = subprocess.Popen(
                video_cmd,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            logging.info(f"Starting audio decoder...")
            self.audio_process = subprocess.Popen(
                audio_cmd,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            # Wait for processes to initialize
            time.sleep(2)

            logging.info(f"Starting muxer...")
            self.muxer_process = subprocess.Popen(
                muxer_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            self.process = self.muxer_process

            # Accept connections with timeout
            video_server.settimeout(30)
            audio_server.settimeout(30)

            logging.info("Waiting for FFmpeg to connect...")
            
            video_conn, _ = video_server.accept()
            logging.info("Video connection established")
            self.video_pipe = video_conn.makefile('wb')
            self._video_ready.set()

            audio_conn, _ = audio_server.accept()
            logging.info("Audio connection established")
            self.audio_pipe = audio_conn.makefile('wb')
            self._audio_ready.set()

            # Close server sockets
            video_server.close()
            audio_server.close()

            # Start stderr capture threads
            threading.Thread(
                target=self._capture_stderr,
                args=(self.video_process, "Video"),
                daemon=True
            ).start()
            threading.Thread(
                target=self._capture_stderr,
                args=(self.audio_process, "Audio"),
                daemon=True
            ).start()
            threading.Thread(
                target=self._capture_stderr,
                args=(self.muxer_process, "Muxer"),
                daemon=True
            ).start()

            logging.info("FFmpeg pipeline started successfully")

        except socket.timeout:
            logging.error("FFmpeg connection timeout")
            self.stop()
            raise RuntimeError("FFmpeg failed to connect within timeout period")
        except Exception as e:
            logging.error(f"Failed to start FFmpeg: {e}")
            self.stop()
            raise

    def _capture_stderr(self, process, name):
        """Capture and log stderr from FFmpeg processes"""
        try:
            for line in iter(process.stderr.readline, b''):
                if self._stop_event.is_set():
                    break
                decoded = line.decode('utf-8', errors='ignore').strip()
                if decoded:
                    log_line = f"FFmpeg ({name}): {decoded}"
                    self.stderr_log.append(log_line)
                    
                    # Log errors and warnings
                    if 'error' in decoded.lower():
                        logging.error(log_line)
                    elif 'warning' in decoded.lower():
                        logging.warning(log_line)
                    else:
                        logging.debug(log_line)
                    
                    # Keep log size manageable
                    if len(self.stderr_log) > 500:
                        self.stderr_log.pop(0)
        except Exception as e:
            logging.debug(f"stderr capture for {name} ended: {e}")

    def wait_ready(self, timeout=30):
        """Wait for both streams to be ready"""
        if not self._video_ready.wait(timeout):
            raise RuntimeError("Video stream not ready within timeout")
        if not self._audio_ready.wait(timeout):
            raise RuntimeError("Audio stream not ready within timeout")
        logging.info("Both streams ready")

    def stop(self):
        """Stop all FFmpeg processes and clean up"""
        logging.info("Stopping FFmpeg processes...")
        self._stop_event.set()
        
        # Close pipes first
        for pipe in [self.video_pipe, self.audio_pipe]:
            if pipe:
                try:
                    pipe.close()
                except Exception:
                    pass

        # Terminate processes
        for p in [self.muxer_process, self.video_process, self.audio_process]:
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                except Exception:
                    pass

        # Clean up key files
        for key_file in self._key_files:
            try:
                if os.path.exists(key_file):
                    os.remove(key_file)
                    logging.debug(f"Removed key file: {key_file}")
            except Exception as e:
                logging.warning(f"Failed to remove key file {key_file}: {e}")

        logging.info("FFmpeg processes stopped")



class HLSDownloader(threading.Thread):
    def __init__(self, hls_url: str, segment_queue: queue.Queue, stream_type: str = "video"):
        super().__init__()
        self.daemon = True
        self._hls_url = hls_url
        self._segment_queue = segment_queue
        self._stream_type = stream_type
        self._session = requests.Session()
        self._stop_event = threading.Event()
        self._downloaded_segments = set()
        self._is_initialized = False
        self._stream_switched = False

    def stop(self):
        self._stop_event.set()

    def run(self):
        logging.info(f"HLSDownloader for {self._stream_type} started")
        while not self._stop_event.is_set():
            try:
                if not self._process_manifest():
                    logging.info(f"{self._stream_type} HLS stream appears to have ended.")
                    break
            except Exception as e:
                logging.warning(f"HLSDownloader ({self._stream_type}) error: {e}. Retrying in 2 seconds.")
                time.sleep(2)
        
        self._segment_queue.put(None)
        logging.info(f"HLSDownloader ({self._stream_type}) thread stopped.")

    def _process_manifest(self):
        try:
            response = self._session.get(self._hls_url, timeout=10)
            response.raise_for_status()
            playlist = m3u8.loads(content=response.text, uri=response.url)

            if not self._stream_switched and (playlist.is_variant or playlist.playlists):
                best_stream = sorted(playlist.playlists, key=lambda p: p.stream_info.bandwidth, reverse=True)[0]
                self._hls_url = best_stream.absolute_uri
                self._stream_switched = True
                logging.info(f"{self._stream_type}: Switched to best stream: {self._hls_url}")
                response = self._session.get(self._hls_url, timeout=10)
                response.raise_for_status()
                playlist = m3u8.loads(content=response.text, uri=response.url)

            # Always check for an initialization segment (EXT-X-MAP) and download if new.
            segment_map = playlist.segment_map
            if isinstance(segment_map, list):
                segment_map = segment_map[0] if segment_map else None

            if segment_map and segment_map.uri:
                init_uri = segment_map.absolute_uri
                if init_uri not in self._downloaded_segments:
                    logging.info(f"{self._stream_type}: Downloading init segment from {init_uri}")
                    init_data = self._session.get(init_uri, timeout=10).content
                    self._segment_queue.put(init_data)
                    self._downloaded_segments.add(init_uri)
            
            for segment in playlist.segments:
                if self._stop_event.is_set():
                    return False
                if segment.uri and segment.absolute_uri not in self._downloaded_segments:
                    media_data = self._session.get(segment.absolute_uri, timeout=10).content
                    self._segment_queue.put(media_data)
                    self._downloaded_segments.add(segment.absolute_uri)
            
            if playlist.is_endlist:
                return False

        except Exception as e:
            raise RuntimeError(f"Could not process HLS manifest: {e}")
        
        time.sleep(playlist.target_duration / 2 if playlist.target_duration else 2)
        return True

class SlingStreamer:
    def __init__(self, channel_id, user_jwt, config):
        self._channel_id = channel_id
        self._user_jwt = user_jwt
        self.config = config
        self._video_segment_queue = queue.Queue(maxsize=100)
        self._audio_segment_queue = queue.Queue(maxsize=100)
        self.sling_client = None
        self.widevine_manager = None
        self.ffmpeg = None
        self.video_downloader = None
        self.audio_downloader = None
        self._stop_event = threading.Event()
        self._cleanup_lock = threading.Lock()

    def run(self, output_queue):
        """Main streaming loop with improved error handling"""
        try:
            # Initialize clients
            from app import SlingClient, WidevineManager, HLSDownloader
            self.sling_client = SlingClient(self._user_jwt, self.config)
            self.widevine_manager = WidevineManager(self.config)

            # Authenticate and get stream URLs
            logging.info(f"Authenticating stream for channel {self._channel_id}")
            stream_url, key_url, temp_jwt = self.sling_client.authenticate_stream(self._channel_id)
            if not stream_url:
                raise RuntimeError("Stream authentication failed")

            # Get audio URL
            audio_url = self.sling_client.derive_audio_url(stream_url)
            logging.info(f"Video URL: {stream_url}")
            logging.info(f"Audio URL: {audio_url}")

            # Get decryption keys
            pssh = self.sling_client.get_pssh_from_hls(key_url)
            if not pssh:
                raise RuntimeError("Failed to retrieve PSSH")

            keys = self.widevine_manager.get_decryption_keys(pssh, temp_jwt)
            if not keys:
                raise RuntimeError("Failed to get decryption keys")
            
            logging.info(f"Retrieved {len(keys)} decryption key(s)")

            # Start downloaders
            self.video_downloader = HLSDownloader(stream_url, self._video_segment_queue, "video")
            self.audio_downloader = HLSDownloader(audio_url, self._audio_segment_queue, "audio")
            self.video_downloader.start()
            self.audio_downloader.start()

            # Start FFmpeg immediately
            self.ffmpeg = FFmpegProcess(keys_dict=keys)
            self.ffmpeg.start()
            self.ffmpeg.wait_ready(timeout=30)

            # Pass the segment queues directly to the feeder threads.
            # The feeder will now handle getting the first segment.
            video_buffer = []
            audio_buffer = []

            # Start feeder threads
            video_feeder = threading.Thread(
                target=self._feed_stream,
                args=(video_buffer, self._video_segment_queue, self.ffmpeg.video_pipe, "video"),
                daemon=True
            )
            audio_feeder = threading.Thread(
                target=self._feed_stream,
                args=(audio_buffer, self._audio_segment_queue, self.ffmpeg.audio_pipe, "audio"),
                daemon=True
            )
            video_feeder.start()
            audio_feeder.start()

            # Start output reader
            stdout_reader = threading.Thread(
                target=self._read_output,
                args=(output_queue,),
                daemon=True
            )
            stdout_reader.start()

            logging.info("Streaming started successfully")

            # Monitor health
            while not self._stop_event.is_set():
                if self.ffmpeg and self.ffmpeg.process.poll() is not None:
                    if not self._stop_event.is_set():
                        logging.error("FFmpeg process has exited unexpectedly.")
                        if self.ffmpeg.stderr_log:
                            logging.error("FFmpeg stderr:")
                            for line in self.ffmpeg.stderr_log:
                                logging.error(line)
                    break
                if self.video_downloader and not self.video_downloader.is_alive():
                    logging.warning("Video downloader thread has stopped.")
                    break
                if self.audio_downloader and not self.audio_downloader.is_alive():
                    logging.warning("Audio downloader thread has stopped.")
                    break
                
                self._stop_event.wait(0.5)

        except Exception as e:
            logging.error(f"Streaming error: {e}", exc_info=True)
            self._stop_event.set()
        finally:
            output_queue.put(None)
            self.cleanup()

    def _feed_stream(self, initial_buffer, segment_queue, pipe, name):
        """Feed segments to FFmpeg pipe"""
        logging.info(f"{name} feeder started")
        try:
            while not self._stop_event.is_set():
                try:
                    segment = segment_queue.get(timeout=10)
                    if segment is None:
                        logging.warning(f"{name} downloader signaled end")
                        break
                    pipe.write(segment)
                    pipe.flush()
                except queue.Empty:
                    logging.warning(f"No {name} segment in 10 seconds")
                except (BrokenPipeError, IOError) as e:
                    if not self._stop_event.is_set():
                        logging.error(f"{name} pipe error: {e}")
                    break

        except Exception as e:
            logging.error(f"{name} feeder error: {e}")
        finally:
            try:
                pipe.close()
            except Exception:
                pass
            logging.info(f"{name} feeder stopped")

    def _read_output(self, output_queue):
        """Read muxed output from FFmpeg"""
        logging.info("Output reader started")
        try:
            while not self._stop_event.is_set():
                chunk = self.ffmpeg.process.stdout.read(8192)
                if not chunk:
                    break
                output_queue.put(chunk)
        except Exception as e:
            if not self._stop_event.is_set():
                logging.error(f"Output reader error: {e}")
        finally:
            logging.info("Output reader stopped")

    def cleanup(self):
        """Clean up all resources"""
        with self._cleanup_lock:
            logging.info("Cleaning up streamer resources...")
            self._stop_event.set()
            
            if self.video_downloader:
                self.video_downloader.stop()
            if self.audio_downloader:
                self.audio_downloader.stop()
            if self.ffmpeg:
                self.ffmpeg.stop()
            
            logging.info("Cleanup complete")

# ==============================================================================
# --- 3. WEB SERVER LOGIC ---
# ==============================================================================
app = Flask(__name__)
@app.route('/')
def index(): return render_template('index.html')
@app.route('/api/channels')
def get_channels():
    try:
        config = CoreConfig(wvd_path=WVD_FILE)
        client = SlingClient(SLING_JWT, config)
        channels = client.get_channel_list()
        if not channels: return jsonify({"error": "Failed to fetch channel list. Check JWT and logs."}), 500
        return jsonify(sorted(channels, key=lambda c: c.get('title', '')))
    except Exception as e: logging.error(f"Error fetching channel list: {e}"); return jsonify({"error": "Failed to fetch channel list."}), 500

async def stream_handler(websocket, path):
    channel_id = path.strip('/')
    if not channel_id: await websocket.close(1003, "Channel ID required"); return
    logging.info(f"Client connected for channel: {channel_id}")
    streamer = None
    sync_queue = queue.Queue(maxsize=100)
    try:
        config = CoreConfig(wvd_path=WVD_FILE)
        streamer = SlingStreamer(channel_id, SLING_JWT, config)
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, streamer.run, sync_queue)
        while True:
            chunk = await loop.run_in_executor(None, sync_queue.get)
            if chunk is None: break
            await websocket.send(chunk)
    except websockets.exceptions.ConnectionClosed as e: logging.info(f"Client for {channel_id} disconnected (code: {e.code}).")
    except Exception as e:
        logging.error(f"An error occurred during streaming for {channel_id}: {e}")
        try: await websocket.close(1011, f"Internal server error: {e}")
        except websockets.exceptions.ConnectionClosed: pass
    finally:
        logging.info(f"Cleaning up resources for channel: {channel_id}")
        if streamer: streamer.cleanup()

def run_flask(): app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, use_reloader=False)

async def start_web_server():
    server = await websockets.serve(stream_handler, WEBSOCKET_HOST, WEBSOCKET_PORT)
    logging.info(f"WebSocket server started on ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}")
    logging.info(f"HTTP server started on http://{HTTP_HOST}:{HTTP_PORT}")
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, run_flask)
    await server.wait_closed()

# ==============================================================================
# --- 4. COMMAND-LINE INTERFACE (CLI) LOGIC ---
# ==============================================================================
def list_channels_cli():
    print("Fetching channel list...")
    try:
        config = CoreConfig(wvd_path=WVD_FILE)
        client = SlingClient(SLING_JWT, config)
        channels = client.get_channel_list()
        if not channels: print("\nNo channels found or failed to fetch. Check JWT and logs."); return
        print("\n--- Available Channels ---")
        for channel in sorted(channels, key=lambda c: c.get('title', '')):
            print(f"  ID: {channel.get('id', 'N/A'):<20} | Title: {channel.get('title', 'No Title')}")
        print("--------------------------\n")
    except Exception as e: logging.error(f"Failed to retrieve channel list: {e}")

def stream_channel_cli(channel_id, output_file):
    streamer = None; output_handle = None
    try:
        if output_file:
            logging.info(f"Streaming to file: {output_file}")
            output_handle = open(output_file, 'wb')
        else:
            logging.info(f"Streaming to stdout. Pipe to a player like ffplay or vlc (e.g., python app.py -c ... | ffplay -).")
            output_handle = sys.stdout.buffer
        config = CoreConfig(wvd_path=WVD_FILE)
        streamer = SlingStreamer(channel_id, SLING_JWT, config)
        output_queue = queue.Queue()
        streamer_thread = threading.Thread(target=streamer.run, args=(output_queue,), daemon=True)
        streamer_thread.start()
        while True:
            chunk = output_queue.get()
            if chunk is None: logging.info("End of stream. Exiting."); break
            try: output_handle.write(chunk); output_handle.flush()
            except BrokenPipeError: logging.warning("Output pipe broken. Player likely closed."); break
    except KeyboardInterrupt: logging.info("\nInterrupted by user. Shutting down.")
    except Exception as e: logging.critical(f"A critical error occurred: {e}")
    finally:
        logging.info("Cleaning up...")
        if streamer: streamer.cleanup()
        if output_file and output_handle: output_handle.close()
        logging.info("CLI finished.")

# ==============================================================================
# --- 5. APPLICATION STARTUP ---
# ==============================================================================
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Sling TV Web/CLI Streamer. Runs as a web server by default.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-l', '--list-channels', action='store_true', help='List all available channels and exit.')
    group.add_argument('-c', '--channel', metavar='ID', type=str, help='The ID of the channel to stream to stdout.')
    parser.add_argument('-o', '--output', metavar='FILE', type=str, help='(Optional, with --channel) Save stream to file.')
    args = parser.parse_args()

    if not SLING_JWT: logging.critical("FATAL ERROR: SLING_JWT environment variable not set!"); sys.exit(1)
    if not os.path.exists(WVD_FILE): logging.critical(f"FATAL ERROR: {WVD_FILE} not found!"); sys.exit(1)

    if args.list_channels:
        logging.info("--- Starting in CLI mode: List Channels ---")
        list_channels_cli()
    elif args.channel:
        logging.info(f"--- Starting in CLI mode: Stream Channel {args.channel} ---")
        stream_channel_cli(args.channel, args.output)
    else:
        logging.info("--- No CLI arguments detected. Starting Web Server mode. ---")
        try:
            asyncio.run(start_web_server())
        except KeyboardInterrupt:
            logging.info("\nServers shutting down.")
        except OSError as e:
            logging.critical(f"FATAL STARTUP ERROR: {e}")
            sys.exit(1)
