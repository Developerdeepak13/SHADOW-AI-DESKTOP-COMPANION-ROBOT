<div align="center">SHADOW
AI Voice Teaching Assistant & Desktop Companion
A voice-first AI companion with a visual expression interface, persistent memory, study tools, and hardware interaction.
</div>Overview
SHADOW is a Python-based AI voice assistant and desktop companion powered by the Gemini Live API. It supports real-time voice conversations, teaching and mock-interview modes, persistent conversation memory, study tools, camera-based visual questions, and interaction with connected hardware.
Features
Voice interaction
- Real-time voice conversations using the Gemini Live model.
- Microphone audio capture and speaker audio playback.
- Audio playback and microphone handling designed to keep interaction responsive.
- Active/passive session handling and automatic reconnection when a Gemini Live session ends.
AI assistant and learning modes
- Normal assistant: conversational assistance and tool use.
- Teaching mentor: explains topics step by step and checks understanding.
- Mock interview: technical, HR/behavioral, and mixed interview practice, one question at a time.
- Quiz mode: multiple-choice quiz questions with answer checking.
Memory and productivity
- SQLite-based conversation history and long-term memory.
- Saves user facts and consolidates session memories.
- Create and cancel reminders; due reminders can be announced by SHADOW.
- Pomodoro-style study sessions with focus and break cycles, pause/resume/stop controls, and progress tracking.
Tools and information
- Current time lookup.
- Weather lookup using Open-Meteo.
- Web search using Tavily when a Tavily API key is configured.
- Camera capture for visual questions through the look_at_camera tool.
Visual interface and hardware interaction
- Full-screen Tkinter expression window with animated GIF expressions.
- Capacitive touch sensor support on Raspberry Pi GPIO 16.
- Voice-commanded head movement through an ESP32 HTTP controller (left, right, and center).
Project structure
SHADOW_CODE_ZIP/
├── agent.py                 # SHADOW personality and conversation instructions
├── audio_io.py              # Microphone capture and audio playback
├── camera.py                # Camera frame capture
├── config.py                # Model, audio, database, and environment configuration
├── database.py              # SQLite conversation, memory, reminder, and study data
├── echo_gate.py             # Audio playback feedback hangover helper
├── gui.py                   # Tkinter expression window and GIF animation
├── head_controller.py       # Voice-commanded ESP32 head movement
├── llm_utils.py             # Gemini helper used for memory consolidation
├── main.py                  # Main SHADOW application and session lifecycle
├── memory.py                # Long-term memory extraction and consolidation
├── tools.py                 # Weather, time, search, quiz, reminder, and study tools
├── touch_sensor.py          # Raspberry Pi GPIO touch sensor integration
├── requirements.txt         # Python dependencies
├── run_shadow.sh            # Supervisor script that restarts after unexpected exits
└── run_shadow_once.sh       # Starts main.py once inside the virtual environment
Tech stack
- Language: Python
- Realtime AI: Google Gemini Live (google-genai)
- Memory and local data: SQLite
- GUI and expressions: Tkinter, Pillow, animated GIFs
- Audio: sounddevice, NumPy, PortAudio (and PipeWire playback when available)
- Web search: Tavily
- Weather: Open-Meteo
- Hardware integration: Raspberry Pi GPIO via gpiozero, camera command-line utilities, ESP32 HTTP controller
Setup
1. Clone the repository
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd SHADOW_CODE_ZIP
2. Create and activate a virtual environment
Linux / Raspberry Pi:
python3 -m venv .venv
source .venv/bin/activate
Windows PowerShell:
py -m venv .venv
.venv\Scripts\Activate.ps1
3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
4. Configure environment variables
Create a .env file in the project root:
GEMINI_API_KEY=your_gemini_api_key
TAVILY_API_KEY=your_tavily_api_key
GEMINI_API_KEY is required by config.py. TAVILY_API_KEY is used for web search.
5. Configure the connected hardware
- head_controller.py contains the ESP32 IP address used by the head controller. Set it to the address of your ESP32 on your network.
- The touch sensor code uses GPIO 16 and the gpiozero package on Raspberry Pi.
- camera.py looks for rpicam-still, libcamera-still, or fswebcam to capture an image.
- gui.py loads expression GIF files from a gifs2/ directory next to the Python files. Add the GIF expression assets used by your setup to that directory.
Run SHADOW
The provided shell scripts use the .venv virtual environment.

Run once:
./run_shadow_once.sh
Run with automatic restart after an unexpected exit:
chmod +x run_shadow.sh run_shadow_once.sh
./run_shadow.sh
You can also start the application directly after activating the virtual environment:
python main.py
Security note
- Do not commit .env, API keys, local databases, or other secrets.
- Keep .env local. The repository's .gitignore is configured to ignore .env files and local SQLite database files.
- If a key has ever been committed or shared publicly, rotate it.
Notes
- SHADOW starts directly in its Gemini Live session; wake-word detection is not part of the current main.py startup flow.
- Some hardware-dependent features require the corresponding Raspberry Pi camera, GPIO sensor, ESP32 controller, and local GIF assets to be available and configured.
<div align="center">SHADOW — Your AI voice teaching assistant and desktop companion.
</div>
