# app.py

import os
import sys
import json
import time
import queue
import signal
import socket
import base64
import tempfile
import threading
import subprocess
import asyncio
import logging
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
import jwt
import websockets
from flask import Flask, jsonify, render_template
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

# ==============================================================================
# --- 1. CENTRALIZED CONFIGURATION ---
# ==============================================================================
# All user-configurable settings are here.
WVD_FILE = 'WVD.wvd'
HTTP_HOST = '0.0.0.0'
HTTP_PORT = 8000
WEBSOCKET_HOST = '0.0.0.0'
WEBSOCKET_PORT = 8765

# Configure logging for better feedback
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# Read the Sling JWT from an environment variable for security
SLING_JWT = os.environ.get("SLING_JWT")


# ==============================================================================
# --- 2. CORE STREAMING LOGIC (Classes from sling_core.py) ---
# ==============================================================================
# These classes are the engine of the tool.

class CoreConfig:
    def __init__(self, wvd_path):
        self.WVD_PATH = wvd_path
        self.SLING_STREAM_AUTH_URL = 'https://p-streamauth.movetv.com/stream/auth'
        self.SLING_WIDEVINE_PROXY_URL = 'https://p-drmwv.movetv.com/widevine/proxy'
        self.SLING_CHANNELS_URL = 'https://p-cmwnext.movetv.com/pres/grid_guide_a_z'
        self.WIDEVINE_PSSH_SYSTEM_ID = 'edef8ba9-79d6-4ace-a3c8-27dcd51d21ed'
        self.SEGMENT_FETCH_LOOKBEHIND = 5
        self.DASH_MPD_REFRESH_INTERVAL = 5

class KeyManager:
    # ... (No changes needed, class is self-contained)
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
    def get_key_source_path(self): return self._key_source_path
    def provide_keys(self, keys):
        key_data = "\n".join([f"{kid}:{cek}" for kid, cek in keys.items()])
        try:
            if self._is_unix:
                with open(self._key_source_path, 'w') as pipe: pipe.write(key_data)
            else:
                self._temp_file_handle.write(key_data)
                self._temp_file_handle.close()
        except Exception as e:
            logging.error(f"KeyManager Error: Failed to provide keys: {e}")
    def cleanup(self):
        if self._key_source_path and os.path.exists(self._key_source_path):
            try: os.remove(self._key_source_path)
            except OSError as e: logging.error(f"KeyManager Cleanup Error: {e}")

class SlingClient:
    # ... (No changes needed, class is self-contained)
    def __init__(self, user_jwt, config):
        self.config=config; self._user_jwt=user_jwt; self._subscriber_id=jwt.decode(self._user_jwt,options={"verify_signature":False})['prof']
    def get_channel_list(self):
        headers={'authorization':f'Bearer {self._user_jwt}'}
        response=requests.get(self.config.SLING_CHANNELS_URL,headers=headers).json()
        channels=[]
        for ribbon in response.get('special_ribbons',[]):
            for tile in ribbon.get('tiles',[]): channels.append({'title':tile.get('title'),'id':tile.get('href','').split('/')[-1]})
        return channels
    def authenticate_stream(self,channel_id):
        headers={'authorization':f'Bearer {self._user_jwt}','Content-Type':'application/json'}
        payload={'subscriber_id':self._subscriber_id,'drm':'widevine','qvt':f'https://cbd46b77.cdn.cms.movetv.com/playermetadata/sling/v1/api/channels/{channel_id}/current/schedule.qvt','os_version':'10','device_name':'browser','os_name':'Windows','brand':'sling','account_status':'active','advertiser_id':None,'support_mode':'false','ssai_vod':'true','ssai_dvr':'true',}
        try:
            response=requests.post(self.config.SLING_STREAM_AUTH_URL,headers=headers,json=payload,timeout=20); response.raise_for_status(); data=response.json()
            manifest_url=data.get('ssai_manifest','').split('?')[0]; temp_jwt=data.get('jwt')
            if not manifest_url or not temp_jwt: raise ValueError("Stream auth response missing data.")
            return manifest_url,temp_jwt
        except(requests.RequestException,ValueError)as e: logging.error(f"Error authenticating stream: {e}"); return None,None
    def get_pssh(self,mpd_url):
        try:
            response=requests.get(mpd_url,timeout=10); response.raise_for_status(); root=ET.fromstring(response.text); ns={'mpd':'urn:mpeg:dash:schema:mpd:2011','cenc':'urn:mpeg:cenc:2013'}
            pssh_xpath=f".//mpd:ContentProtection[@schemeIdUri='urn:uuid:{self.config.WIDEVINE_PSSH_SYSTEM_ID}']//cenc:pssh"
            pssh_element=root.find(pssh_xpath,ns)
            if pssh_element is not None and pssh_element.text: return pssh_element.text.strip()
            raise ValueError("PSSH data not found in MPD.")
        except(requests.RequestException,ET.ParseError,ValueError)as e: logging.error(f"Error getting PSSH from MPD: {e}"); return None

class WidevineManager:
    # ... (No changes needed, class is self-contained)
    def __init__(self,config):
        self.config=config
        if not os.path.exists(config.WVD_PATH): raise FileNotFoundError(f"WVD file not found at '{config.WVD_PATH}'")
        self._device=Device.load(config.WVD_PATH)
    def get_decryption_keys(self,pssh_b64,channel_id,temp_jwt):
        try:
            pssh=PSSH(base64.b64decode(pssh_b64)); cdm=Cdm.from_device(self._device); session_id=cdm.open(); challenge=cdm.get_license_challenge(session_id,pssh)
            headers={'Authorization':f'Bearer {temp_jwt}','Env':'production','Channel-Id':channel_id,'Content-Type':'application/json',}
            payload={"message":list(challenge)}
            response=requests.post(self.config.SLING_WIDEVINE_PROXY_URL,headers=headers,json=payload,timeout=15); response.raise_for_status(); cdm.parse_license(session_id,response.content)
            keys={key.kid.hex:key.key.hex for key in cdm.get_keys(session_id)if key.type=='CONTENT'}
            cdm.close(session_id)
            if not keys: raise ValueError("No content keys were returned.")
            return keys
        except(requests.RequestException,ValueError,FileNotFoundError)as e: logging.error(f"Error during Widevine license acquisition: {e}"); return{}

class FFmpegProcess:
    # ... (No changes needed, class is self-contained)
    def __init__(self,key_source_path): self._key_source_path=key_source_path; self.process=None
    def start(self):
        cmd=['ffmpeg','-y','-i','pipe:0','-decryption_key_file',self._key_source_path,'-c:v','copy','-c:a','copy','-f','mp4','-movflags','frag_keyframe+empty_moov+default_base_moof','pipe:1']
        self.process=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=10**8)
    def stop(self):
        if self.process and self.process.poll()is None:
            try: self.process.stdin.close(); self.process.terminate(); self.process.wait(timeout=5)
            except(IOError,BrokenPipeError,subprocess.TimeoutExpired): self.process.kill()
            logging.info("FFmpeg process stopped.")

class DashDownloader(threading.Thread):
    # ... (No changes needed, class is self-contained)
    def __init__(self, mpd_url: str, segment_queue: queue.Queue, config: CoreConfig):
        super().__init__(); self.daemon=True; self._mpd_url=mpd_url; self._segment_queue=segment_queue; self.config=config
        self._session=requests.Session(); self._initialized_reps=set(); self._last_segment_nums={}; self._stop_event=threading.Event()
    def stop(self): self._stop_event.set()
    def run(self):
        while not self._stop_event.is_set():
            try: self._process_manifest()
            except Exception as e: logging.warning(f"Downloader error: {e}")
            time.sleep(self.config.DASH_MPD_REFRESH_INTERVAL)
        logging.info("DashDownloader thread stopped.")
    def _process_manifest(self):
        response=self._session.get(self._mpd_url,timeout=10); response.raise_for_status(); root=ET.fromstring(response.text)
        ns={'mpd':'urn:mpeg:dash:schema:mpd:2011'}; availability_start_time=requests.utils.parse_datetime(root.get('availabilityStartTime'))
        period=root.find('mpd:Period',ns);
        if not period: return
        base_url_elem=period.find('mpd:BaseURL',ns)or root.find('mpd:BaseURL',ns)
        base_url=base_url_elem.text if base_url_elem and base_url_elem.text else self._mpd_url.rsplit('/',1)[0]+'/'
        for adapt_set in period.findall('mpd:AdaptationSet',ns): self._process_adaptation_set(adapt_set,base_url,availability_start_time,ns)
    def _process_adaptation_set(self,adapt_set,base_url,availability_start_time,ns):
        rep=adapt_set.find('mpd:Representation',ns)
        if not rep:return
        rep_id=rep.get('id'); content_type=adapt_set.get('contentType'); rep_key=f"{content_type}-{rep_id}"
        seg_template=adapt_set.find('mpd:SegmentTemplate',ns)
        if not seg_template: return
        if rep_key not in self._initialized_reps:
            init_template=seg_template.get('initialization')
            init_url=urljoin(base_url,init_template.replace('$RepresentationID$',rep_id))
            init_data=self._session.get(init_url,timeout=10).content; self._segment_queue.put(init_data)
            self._initialized_reps.add(rep_key)
        timescale=int(seg_template.get('timescale',1)); seg_duration_timescale=int(seg_template.get('duration')); seg_duration_sec=seg_duration_timescale/timescale if timescale else 0
        if seg_duration_sec<=0:return
        current_utc_time=requests.utils.parse_datetime(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())); time_since_start=(current_utc_time-availability_start_time).total_seconds()
        current_segment_num=int(time_since_start/seg_duration_sec); last_downloaded=self._last_segment_nums.get(rep_key,current_segment_num-self.config.SEGMENT_FETCH_LOOKBEHIND)
        media_template=seg_template.get('media')
        for seg_num in range(last_downloaded+1,current_segment_num+1):
            if self._stop_event.is_set(): return
            hex_seg_num=f"{seg_num:08x}"
            media_url=urljoin(base_url,media_template.replace('$RepresentationID$',rep_id).replace('$Number%08x$',hex_seg_num))
            media_data=self._session.get(media_url,timeout=10).content; self._segment_queue.put(media_data)
        self._last_segment_nums[rep_key]=current_segment_num

class SlingStreamer:
    # ... (No changes needed, class is self-contained)
    def __init__(self, channel_id, user_jwt, config):
        self._channel_id=channel_id; self._user_jwt=user_jwt; self.config=config; self._segment_queue=queue.Queue(maxsize=100)
        self.key_manager=KeyManager(); self.sling_client=SlingClient(self._user_jwt,self.config); self.widevine_manager=WidevineManager(self.config)
        self.ffmpeg=None; self.downloader=None; self._is_running=True
    def run(self, output_queue: queue.Queue):
        try:
            self.key_manager.setup()
            mpd_url, temp_jwt = self.sling_client.authenticate_stream(self._channel_id)
            if not mpd_url: raise RuntimeError("Failed to authenticate stream.")
            pssh = self.sling_client.get_pssh(mpd_url)
            if not pssh: raise RuntimeError("Failed to retrieve PSSH.")
            keys = self.widevine_manager.get_decryption_keys(pssh, self._channel_id, temp_jwt)
            if not keys: raise RuntimeError("Failed to retrieve decryption keys.")
            if os.name!='posix': self.key_manager.provide_keys(keys)
            self.ffmpeg=FFmpegProcess(self.key_manager.get_key_source_path()); self.ffmpeg.start()
            if os.name=='posix': threading.Thread(target=self.key_manager.provide_keys,args=(keys,),daemon=True).start()
            self.downloader=DashDownloader(mpd_url,self._segment_queue,self.config); self.downloader.start()
            feeder_thread=threading.Thread(target=self._feed_ffmpeg,daemon=True); feeder_thread.start()
            logging.info(f"Stream is now live for channel {self._channel_id}")
            while self._is_running and self.ffmpeg and self.ffmpeg.process.poll() is None:
                chunk = self.ffmpeg.process.stdout.read(8192)
                if not chunk: break
                output_queue.put(chunk)
        except Exception as e: logging.critical(f"A critical error occurred in SlingStreamer for channel {self._channel_id}: {e}")
        finally:
            logging.info(f"Streamer run loop for {self._channel_id} finished. Cleaning up.")
            output_queue.put(None) # Sentinel value to signal the end
            self.cleanup()
    def _feed_ffmpeg(self):
        while self._is_running and self.ffmpeg and self.ffmpeg.process.poll() is None:
            try:
                segment_data = self._segment_queue.get(timeout=10)
                self.ffmpeg.process.stdin.write(segment_data); self.ffmpeg.process.stdin.flush()
            except queue.Empty: continue
            except(IOError,AttributeError,BrokenPipeError): logging.warning("FFmpeg feeder thread: pipe broke, exiting."); break
        logging.info("FFmpeg feeder thread stopped.")
    def cleanup(self):
        logging.info(f"Initiating cleanup for channel {self._channel_id}...")
        self._is_running = False
        if self.downloader: self.downloader.stop()
        if self.ffmpeg: self.ffmpeg.stop()
        if self.key_manager: self.key_manager.cleanup()
        logging.info(f"Cleanup for {self._channel_id} complete.")


# ==============================================================================
# --- 3. WEB SERVER AND WEBSOCKET LOGIC ---
# ==============================================================================

app = Flask(__name__)

@app.route('/')
def index():
    """Serves the main web page."""
    return render_template('index.html')

@app.route('/api/channels')
def get_channels():
    """API to fetch the list of available channels."""
    try:
        config = CoreConfig(wvd_path=WVD_FILE)
        client = SlingClient(SLING_JWT, config)
        channels = client.get_channel_list()
        return jsonify(sorted(channels, key=lambda c: c.get('title', '')))
    except Exception as e:
        logging.error(f"Error fetching channel list: {e}")
        return jsonify({"error": "Failed to fetch channel list. Check JWT and server logs."}), 500

async def stream_handler(websocket, path):
    """Handles a single client WebSocket connection, creating a dedicated stream."""
    channel_id = path.strip('/').split('/')[-1]
    if not channel_id:
        logging.warning("Client connected without a channel ID. Closing.")
        await websocket.close(1003, "Channel ID required")
        return

    logging.info(f"Client connected for channel: {channel_id}")
    streamer = None
    sync_queue = queue.Queue(maxsize=100)

    try:
        config = CoreConfig(wvd_path=WVD_FILE)
        streamer = SlingStreamer(channel_id, SLING_JWT, config)
        loop = asyncio.get_running_loop()
        # Run the blocking streamer code in a separate thread
        loop.run_in_executor(None, streamer.run, sync_queue)

        while True:
            # Bridge the synchronous queue to the async WebSocket handler
            chunk = await loop.run_in_executor(None, sync_queue.get)
            if chunk is None:
                logging.info(f"End of stream signal received for {channel_id}.")
                break
            await websocket.send(chunk)

    except websockets.exceptions.ConnectionClosed as e:
        logging.info(f"Client for {channel_id} disconnected (code: {e.code}).")
    except Exception as e:
        logging.error(f"An error occurred during streaming for {channel_id}: {e}")
        await websocket.close(1011, f"Internal server error: {e}")
    finally:
        logging.info(f"Cleaning up resources for channel: {channel_id}")
        if streamer:
            streamer.cleanup()

# ==============================================================================
# --- 4. APPLICATION STARTUP ---
# ==============================================================================

async def main():
    """Starts both the WebSocket server and the Flask server."""
    websocket_server = await websockets.serve(stream_handler, WEBSOCKET_HOST, WEBSOCKET_PORT)
    logging.info(f"WebSocket server started on ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}")

    # Run Flask in a separate thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False),
        daemon=True
    )
    flask_thread.start()
    logging.info(f"HTTP server started on http://{HTTP_HOST}:{HTTP_PORT}")

    await websocket_server.wait_closed()

if __name__ == '__main__':
    # --- Startup Pre-flight Checks ---
    if not SLING_JWT:
        logging.critical("FATAL ERROR: SLING_JWT environment variable not set!")
        sys.exit(1)
    if not os.path.exists(WVD_FILE):
        logging.critical(f"FATAL ERROR: WVD file '{WVD_FILE}' not found!")
        sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("\nServers shutting down.")
