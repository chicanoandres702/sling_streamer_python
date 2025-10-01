# Sling TV Web Player

<p align="center">
  <img src="https-user-images.githubusercontent.com/12345/123456789-abcdef.png" alt="Sling Player Screenshot" width="800">
  <br/>
  <em>A self-hosted, browser-based streaming interface for your Sling TV account, with full remote control support.</em>
</p>

<p align="center">
  <img alt="Python Version" src="https://img.shields.io/badge/python-3.8+-blue.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
  <a href="https://github.com/your-username/sling-player/pulls"><img alt="PRs Welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg"></a>
</p>

This application provides a clean, "10-foot UI" for streaming your Sling TV channels on any device with a modern web browser. It uses a high-performance Python backend that handles authentication, decryption, and on-demand streaming via WebSockets for a smooth, low-latency experience.

---

## ✨ Features

-   **📺 Modern Web Interface:** Access your channels from any browser on your network.
-   **⚡ On-Demand Streaming:** A stream is only active when a client is connected, saving server resources.
-   **🚀 Low-Latency Playback:** Utilizes a direct WebSocket-to-MSE pipeline, avoiding the higher latency of HLS/DASH for a more "live" feel.
-   **🎮 Full Remote Control:** Navigate the UI, select channels, seek, and toggle fullscreen using only a keyboard or remote.
-   **🧩 Single-File Backend:** The entire server logic is consolidated into a single, easy-to-update `app.py` file.
-   **✅ Easy Setup:** Get up and running in minutes with minimal configuration.

---

## 🏗️ Architecture Overview

The system is designed for efficiency. A client's request initiates a dedicated, end-to-end streaming pipeline that is torn down automatically upon disconnection.

```
                  [Sling TV Servers]
                        ▲
                        │ (Encrypted DASH Segments)
                        │
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
                        │
│                 [Python Backend]            │
                        │
│     SlingStreamer ◀────▶ FFmpeg (decrypts)  │
│          (manages)      │
└ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
                   │ (Decrypted fMP4 Chunks)
                   ▼
           [WebSocket Server]
                   │
                   │ (Binary Video Data)
                   ▼
        [Browser Frontend]
 (Media Source Extensions API)
```

---

## 🚀 Getting Started

Follow these steps to set up and run your own instance of the Sling Player.

### Prerequisites

1.  **Python 3.8+**
2.  **FFmpeg:** Must be installed and available in your system's PATH.
3.  **A `WVD.wvd` File:** Your personal Widevine device file is required for DRM decryption.

### Installation

For experienced users, here's the quick start:

```bash
git clone https://github.com/your-username/sling-player.git
cd sling-player
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export SLING_JWT='your_jwt_here'
# Place your WVD.wvd file in this directory
python app.py
```

---

### Step-by-Step Installation Guide

**1. Clone the Repository**
```bash
git clone https://github.com/your-username/sling-player.git
cd sling-player
```

**2. Place Your WVD File**
Place your `WVD.wvd` file into the root of the `sling-player` directory you just cloned.

**3. Install Dependencies**
It's highly recommended to use a Python virtual environment.
```bash
# Create and activate the environment
python -m venv venv
source venv/bin/activate  # On Windows, use: .\venv\Scripts\activate

# Install required packages
pip install -r requirements.txt
```

**4. Set the `SLING_JWT` Environment Variable**
This is the most critical step. The application needs your Sling TV JWT for authentication.

<details>
  <summary><b>Click here for instructions on how to get your JWT</b></summary>

  1.  Open your browser (Chrome/Firefox recommended) and log in to `sling.com`.
  2.  Open the Developer Tools (usually the `F12` key).
  3.  Go to the **Application** (in Chrome) or **Storage** (in Firefox) tab.
  4.  Under the `Local Storage` section, select the entry for `https://www.sling.com`.
  5.  Find the key named `persist:root`. Its value will be a very large block of JSON text.
  6.  Copy the entire value and paste it into a text editor.
  7.  Search for the `"jwt"` field within that text. Copy the long string that is its value (inside the double quotes). This is your JWT.
</details>

Now, set this value as an environment variable in your terminal:

*   **macOS / Linux:**
    ```bash
    export SLING_JWT='paste_your_long_jwt_string_here'
    ```
*   **Windows (Command Prompt):**
    ```cmd
    set SLING_JWT="paste_your_long_jwt_string_here"
    ```

---

## 🎮 Usage & Controls

**1. Run the Server**
Make sure your virtual environment is active and the JWT is set, then run:
```bash
python app.py
```

**2. Access the Player**
Open your web browser and navigate to:
**`http://127.0.0.1:8000`**

### Remote Control Guide

The player has two focus modes: **Sidebar** and **Player**. A glowing blue border indicates which area is currently active.

| Key | Sidebar Action (Default) | Player Action |
| :--- | :--- | :--- |
| **⬆️ / ⬇️** | Navigate channel list | *N/A* |
| **`Enter`** | Start streaming selected channel | *N/A* |
| **⬅️ / ➡️** | *N/A* | Seek backward/forward 10s |
| **`Space`** | *N/A* | Play / Pause |
| **`f`** | *N/A* | Toggle Fullscreen |
| **`Backspace`**| *N/A* | **Return focus to Sidebar** |

---

## ⚙️ Configuration

All major configuration variables (ports, hostnames, WVD path) are located at the top of the `app.py` file for easy modification.

```python
# --- 1. CENTRALIZED CONFIGURATION ---
WVD_FILE = 'WVD.wvd'
HTTP_HOST = '0.0.0.0'
HTTP_PORT = 8000
WEBSOCKET_HOST = '0.0.0.0'
WEBSOCKET_PORT = 8765
```

## 🤔 Troubleshooting

-   **Server exits immediately on start:** You have likely forgotten to set the `SLING_JWT` environment variable or the `WVD.wvd` file is missing. The terminal will show a `FATAL ERROR` message.
-   **Channel list does not load:** Your `SLING_JWT` is likely expired or invalid. Follow the steps again to get a fresh one.
-   **Stream connects but video is black:** This is almost always a `mimeCodec` mismatch. The stream's video/audio codec does not match the one hardcoded in `templates/index.html`. You may need to inspect the channel's DASH manifest (`.mpd`) to find the correct `codecs="..."` string and update it in the HTML file.

---

## 📜 License

This project is licensed under the MIT License. See the `LICENSE` file for details.

## ⚠️ Disclaimer

This project is intended for educational and personal use only. Accessing and restreaming content in this manner may violate the Sling TV Terms of Service. The developers of this project are not responsible for how it is used. Please use it responsibly and at your own risk.
