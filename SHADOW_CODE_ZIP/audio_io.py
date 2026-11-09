import asyncio
import queue
import threading
import subprocess
import time
import os
import shutil
from gui import expression_management
import numpy as np
import sounddevice as sd

from google.genai import types

from config import (
    INPUT_SAMPLE_RATE,
    INPUT_CHANNELS,
    GEMINI_INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    OUTPUT_CHANNELS,
    CHUNK_SIZE,
)


# ============================================================
# AUDIO PLAYER
# ============================================================

class AudioPlayer:
    """Non-blocking cross-platform PCM audio player.

    Windows uses sounddevice/PortAudio directly. Linux/Raspberry Pi uses
    PipeWire's pw-cat when available, with PortAudio as a fallback.
    Playback is kept off the asyncio event loop so microphone capture stays
    responsive.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self._speaking = False
        self._closed = False
        self._queue = queue.Queue(maxsize=64)
        self._writer_stop = threading.Event()
        self._portaudio_stream = None
        self.stream = None

        use_pipewire = (
            os.name != "nt"
            and shutil.which("pw-cat") is not None
        )

        if use_pipewire:
            try:
                self.stream = subprocess.Popen(
                    [
                        shutil.which("pw-cat"), "--playback", "--raw",
                        "--rate", str(OUTPUT_SAMPLE_RATE),
                        "--channels", str(OUTPUT_CHANNELS),
                        "--format", "s16", "-",
                    ],
                    stdin=subprocess.PIPE,
                    bufsize=0,
                )
                self._writer = threading.Thread(
                    target=self._writer_loop,
                    name="shadow-speaker",
                    daemon=True,
                )
                self._writer.start()
                print("🔊 Speaker started (PipeWire).")
                return
            except (OSError, FileNotFoundError) as exc:
                print(f"⚠️ PipeWire unavailable ({exc}); using PortAudio.")
                self.stream = None

        # Windows-friendly fallback: sounddevice/PortAudio.
        self._portaudio_stream = sd.RawOutputStream(
            samplerate=OUTPUT_SAMPLE_RATE,
            channels=OUTPUT_CHANNELS,
            dtype="int16",
            blocksize=0,
        )
        self._portaudio_stream.start()
        self._writer = threading.Thread(
            target=self._portaudio_writer_loop,
            name="shadow-speaker",
            daemon=True,
        )
        self._writer.start()
        print(
            f"🔊 Speaker started (Windows/PortAudio) "
            f"{OUTPUT_SAMPLE_RATE} Hz, {OUTPUT_CHANNELS} channel(s)."
        )

    def _writer_loop(self):
        while not self._writer_stop.is_set():
            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is None:
                break
            try:
                if self.stream and self.stream.stdin:
                    self.stream.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                with self.lock:
                    self._speaking = False
                break

    def _portaudio_writer_loop(self):
        while not self._writer_stop.is_set():
            try:
                data = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if data is None:
                break
            try:
                if self._portaudio_stream is not None:
                    self._portaudio_stream.write(data)
            except Exception as exc:
                print(f"⚠️ Speaker output error: {exc}")
                with self.lock:
                    self._speaking = False
                break

    def add_audio(self, audio_data: bytes):
        if self._closed or not audio_data:
            return
        try:
            self._queue.put_nowait(bytes(audio_data))
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(bytes(audio_data))
            except queue.Empty:
                pass
        with self.lock:
            self._speaking = True

    def clear(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self.lock:
            self._speaking = False

    @property
    def speaking(self):
        with self.lock:
            return self._speaking

    def stop(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._speaking = False

        self._writer_stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

        if self._writer.is_alive():
            self._writer.join(timeout=1.0)

        if self._portaudio_stream is not None:
            try:
                self._portaudio_stream.stop()
            except Exception:
                pass
            try:
                self._portaudio_stream.close()
            except Exception:
                pass
            self._portaudio_stream = None

        if self.stream is not None:
            try:
                if self.stream.stdin:
                    self.stream.stdin.close()
            except Exception:
                pass
            try:
                self.stream.terminate()
                self.stream.wait(timeout=2)
            except Exception:
                try:
                    self.stream.kill()
                except Exception:
                    pass
            self.stream = None

        print("🔊 Speaker stopped.")


# ============================================================
# MICROPHONE
# ============================================================

async def microphone(
    session,
    stop_event,
    speaking_event,
    tool_event,
    restart_event=None,
):
    """Capture mic audio with a callback and bounded queue.

    The callback does almost no work: it only copies bytes into a small
    queue. The asyncio task performs resampling/network I/O outside the
    PortAudio callback. This avoids the read()/to_thread timing problem that
    was producing repeated input-overflow messages.
    """
    # Larger queue absorbs short scheduling/network delays on Raspberry Pi.
    audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
    loop = asyncio.get_running_loop()
    callback_error = {"value": None}
    last_status_print = {"value": 0.0}

    def callback(indata, frames, time_info, status):
        if status and time.monotonic() - last_status_print["value"] > 5:
            last_status_print["value"] = time.monotonic()
            print(f"⚠️ Audio input status: {status}")
        if stop_event.is_set():
            return
        try:
            data = bytes(indata)
            try:
                audio_queue.put_nowait(data)
            except queue.Full:
                # Drop oldest capture data rather than blocking PortAudio.
                try:
                    audio_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    audio_queue.put_nowait(data)
                except queue.Full:
                    # Never block the PortAudio callback.
                    pass
        except Exception as exc:
            callback_error["value"] = exc

    device = None
    for index, info in enumerate(sd.query_devices()):
        if int(info.get("max_input_channels", 0)) >= INPUT_CHANNELS and "USB PnP Sound Device" in str(info.get("name", "")):
            device = index
            break
    if device is None:
        # Fall back to PortAudio's default input if the USB name changes.
        default_in = sd.default.device[0]
        if default_in is None or int(sd.query_devices(default_in).get("max_input_channels", 0)) < INPUT_CHANNELS:
            raise RuntimeError("No usable microphone input device found.")
        device = default_in

    # USB PnP mic accepts 48 kHz but rejects 16 kHz.
    # Capture at 48 kHz, then downsample to Gemini's 16 kHz below.
    # 2400 frames = 50 ms at 48 kHz and gives PipeWire/USB scheduling
    # more headroom than a very small callback buffer.
    capture_blocksize = 2400

    stream = sd.RawInputStream(
        device=device,
        samplerate=INPUT_SAMPLE_RATE,
        channels=INPUT_CHANNELS,
        dtype="int16",
        blocksize=capture_blocksize,
        latency="high",
        callback=callback,
    )

    stream.start()
    print(f"🎤 Microphone started: device {device}, {INPUT_SAMPLE_RATE} Hz → Gemini {GEMINI_INPUT_SAMPLE_RATE} Hz")

    muted = False
    try:
        while not stop_event.is_set():
            if callback_error["value"] is not None:
                raise callback_error["value"]

            try:
                data = await asyncio.to_thread(audio_queue.get, True, 0.25)
            except queue.Empty:
                continue

            if speaking_event.is_set() or tool_event.is_set():
                if not muted:
                    reason = "Shadow speaking" if speaking_event.is_set() else "tool running"
                    print(f"🔇 Mic muted ({reason})")
                    muted = True
                continue

            if muted:
                print("🎤 Mic resumed.")
                muted = False

            # The known USB mic runs reliably at 48 kHz. Gemini Live input is
            # 16-bit PCM and accepts a declared sample rate; downsample by 3
            # so we send 16 kHz without requiring scipy/libsamplerate.
            samples = np.frombuffer(data, dtype=np.int16)
            if INPUT_SAMPLE_RATE != GEMINI_INPUT_SAMPLE_RATE:
                ratio = INPUT_SAMPLE_RATE // GEMINI_INPUT_SAMPLE_RATE
                if INPUT_SAMPLE_RATE % GEMINI_INPUT_SAMPLE_RATE != 0:
                    raise RuntimeError("Input/Gemini sample-rate ratio must be an integer.")
                samples = samples[::ratio]
                data = samples.astype(np.int16, copy=False).tobytes()

            await session.send_realtime_input(
                audio=types.Blob(
                    data=data,
                    mime_type=f"audio/pcm;rate={GEMINI_INPUT_SAMPLE_RATE}",
                )
            )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        error_text = str(e).lower()
        reconnectable = (
            "goaway" in error_text or "go away" in error_text
            or "session duration" in error_text
            or "connectionclosed" in error_text
            or "1006" in error_text or "1011" in error_text
        )
        if reconnectable and restart_event is not None:
            print(f"🔄 Gemini microphone connection ended: {e}")
            restart_event.set()
        else:
            print(f"❌ Microphone error: {type(e).__name__}: {e}")
            if restart_event is not None:
                restart_event.set()
    finally:
        # Never cancel a blocking stream.read() because there is none. Closing
        # happens from the event-loop task after the callback is no longer used.
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        print("🎤 Microphone stopped.")


# ============================================================
# RECEIVE LOOP
# ============================================================

async def receive_audio(
    session,
    player,
    stop_event,
    speaking_event,
    tool_event,
    on_user_transcript,
    on_assistant_transcript,
    on_turn_complete,
    on_tool_call,
    is_emotion_expression_active=None,
    restart_event=None,
):

    """
    Gemini Live receiver.

    IMPORTANT:
    The current google-genai AsyncLiveSession.receive()
    can finish after turn_complete.

    Therefore we continuously reopen receive()
    until the overall session is stopped.
    """

    print(
        "ð¡ Gemini receiver started."
    )

    try:

        while not stop_event.is_set():

            try:

                # Expression/local-command guard.
                # A local command such as "show me your angry face" must
                # never produce Gemini speech, audio, or tool execution.
                local_command_active = False

                # Expression guard: change SHADOW to speaking only once
                # for each Gemini response, not once per audio chunk.
                speaking_started = False

                async for response in session.receive():

                    if stop_event.is_set():
                        break

                    if response.server_content:

                        server_content = (
                            response.server_content
                        )

                        # -----------------------------------------
                        # MODEL TURN / AUDIO
                        # -----------------------------------------

                        if server_content.model_turn:

                            # Gemini may still generate a response after a
                            # local command was detected. Keep consuming the
                            # server response so turn_complete is still seen,
                            # but DO NOT play or print Gemini's response.
                            if not local_command_active:

                                # Gemini has started a model response.
                                # Trigger the speaking expression only once.
                                if not speaking_started:
                                    speaking_started = True
                                    # When the user just expressed an emotion,
                                    # keep that emotion GIF visible while SHADOW
                                    # talks. Normal turns still use speaking.gif.
                                    emotion_active = bool(
                                        is_emotion_expression_active
                                        and is_emotion_expression_active()
                                    )
                                    if not emotion_active:
                                        expression_management("speaking")

                                for part in (
                                    server_content
                                    .model_turn
                                    .parts
                                ):

                                    if part.inline_data:

                                        audio_data = (
                                            part.inline_data.data
                                        )

                                        if audio_data:
                                            player.add_audio(
                                                audio_data
                                            )
                                            speaking_event.set()

                                    if part.text:

                                        text = (
                                            part.text.strip()
                                        )

                                        if text:
                                            print(
                                                f"\nð¤ Shadow: {text}"
                                            )

                        # -----------------------------------------
                        # INTERRUPTION
                        # -----------------------------------------

                        if server_content.interrupted:

                            print(
                                "\nâ¡ [BARGE-IN]"
                            )

                            player.clear()
                            speaking_event.clear()
                            speaking_started = False

                            # IMPORTANT:
                            # If this turn was a local expression command,
                            # NEVER reset the requested GIF to neutral.
                            # The expression must remain on screen.
                            if not local_command_active:
                                emotion_active = bool(
                                    is_emotion_expression_active
                                    and is_emotion_expression_active()
                                )
                                if not emotion_active:
                                    expression_management("neutral")

                        # -----------------------------------------
                        # USER TRANSCRIPTION
                        # -----------------------------------------

                        if (
                            server_content
                            .input_transcription
                        ):

                            text = (
                                server_content
                                .input_transcription
                                .text
                            )

                            if text:

                                handled_locally = await on_user_transcript(
                                    text
                                )

                                # The callback returns True for commands
                                # that were completely handled locally.
                                # From this point until turn_complete,
                                # suppress Gemini audio/text/tool output.
                                if handled_locally:
                                    local_command_active = True
                                    player.clear()
                                    speaking_event.clear()
                                    speaking_started = False

                        # -----------------------------------------
                        # OUTPUT TRANSCRIPTION
                        # -----------------------------------------

                        if (
                            server_content
                            .output_transcription
                        ) and not local_command_active:

                            text = (
                                server_content
                                .output_transcription
                                .text
                            )

                            if text:

                                await on_assistant_transcript(
                                    text
                                )

                        # -----------------------------------------
                        # TURN COMPLETE
                        # -----------------------------------------

                        if server_content.turn_complete:

                            print(
                                "\nâ Turn complete."
                            )

                            speaking_event.clear()
                            speaking_started = False

                            await on_turn_complete()

                            # Critical multi-turn workaround:
                            # break only this receive() iterator,
                            # then reopen it in the outer while loop.

                            break

                    # ---------------------------------------------
                    # TOOL CALL
                    # ---------------------------------------------

                    if response.tool_call:

                        # A local command must never reach tools such as
                        # look_at_camera. The expression is already shown.
                        if local_command_active:
                            continue

                        print(
                            "\nð ï¸ Gemini requested tool(s)."
                        )

                        tool_event.set()

                        try:

                            function_calls = (
                                response
                                .tool_call
                                .function_calls
                            )

                            await on_tool_call(
                                function_calls
                            )

                        finally:

                            tool_event.clear()

                if stop_event.is_set():
                    break

                print(
                    "ð¡ Gemini receive() turn ended."
                )

                await asyncio.sleep(
                    0.01
                )

            except asyncio.CancelledError:
                raise

            except Exception as e:

                import traceback

                error_text = str(e).lower()

                is_reconnectable = (
                    "goaway" in error_text
                    or "go away" in error_text
                    or "session duration" in error_text
                    or "failed to close the connection after receiving"
                    in error_text
                    or "keepalive ping timeout" in error_text
                    or "connectionclosederror" in error_text
                    or "abnormal closure" in error_text
                    or "1006" in error_text
                    or "1011" in error_text
                )

                if is_reconnectable and restart_event is not None:
                    print(
                        "\nð Gemini Live connection dropped."
                        " Reconnecting the Live session..."
                    )

                    # Drop any partially queued model audio before
                    # the old connection is closed.
                    player.clear()
                    speaking_event.clear()
                    speaking_started = False
                    expression_management("neutral")
                    restart_event.set()
                    break

                print(
                    "\nâ Gemini receive() error:"
                )

                print(
                    f"Type: {type(e).__name__}"
                )

                print(
                    f"Error: {e!r}"
                )

                traceback.print_exc()

                stop_event.set()
                break

    except asyncio.CancelledError:

        print(
            "\nð Gemini receiver cancelled."
        )

        raise

    except Exception as e:

        import traceback

        print(
            "\nâ GEMINI RECEIVER FATAL ERROR"
        )

        print(
            f"Type: {type(e).__name__}"
        )

        print(
            f"Error: {e!r}"
        )

        traceback.print_exc()

        stop_event.set()

    finally:

        speaking_event.clear()
        tool_event.clear()

        print(
            "ð¡ Gemini receiver stopped."
        )
