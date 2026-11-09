"""
SHADOW touch sensor
GPIO 16 -> capacitive touch sensor OUT

Typical wiring:
    Sensor VCC -> Raspberry Pi 3.3V
    Sensor GND -> Raspberry Pi GND
    Sensor OUT -> GPIO 16
"""

import asyncio
import time

from gui import expression_management

TOUCH_GPIO = 16
TOUCH_EXPRESSION = "touch"
TOUCH_EXPRESSION_TIME = 2.0
TOUCH_COOLDOWN = 1.0


class ShadowTouchSensor:
    def __init__(self, session, loop):
        self.session = session
        self.loop = loop
        self.button = None
        self.closed = False
        self._last_touch = 0.0
        self._touch_task = None
        # Optional hooks supplied by main.py to coordinate expression priority.
        self.on_touch_start = None
        self.on_touch_end = None

    def start(self):
        try:
            from gpiozero import Button
        except ImportError:
            print("ℹ️ Touch sensor disabled: install gpiozero on Raspberry Pi.")
            return False

        try:
            # TTP223-style sensors are commonly active-low with pull-up.
            self.button = Button(
                TOUCH_GPIO,
                pull_up=True,
                bounce_time=0.15,
            )
            self.button.when_pressed = self._gpio_pressed
            print(f"👆 Touch sensor started: GPIO {TOUCH_GPIO}")
            return True

        except Exception as exc:
            print(f"⚠️ Touch sensor unavailable: {exc}")
            self.button = None
            return False

    def _gpio_pressed(self):
        if self.closed:
            return

        now = time.monotonic()
        if now - self._last_touch < TOUCH_COOLDOWN:
            return

        self._last_touch = now

        try:
            self._touch_task = asyncio.run_coroutine_threadsafe(
                self.handle_touch(),
                self.loop,
            )
        except RuntimeError:
            pass

    async def handle_touch(self):
        if self.closed:
            return

        print("👆 TOUCH DETECTED!")
        print("🎭 Touch expression → shadow_happy")

        try:
            if callable(self.on_touch_start):
                self.on_touch_start()

            # Immediate on-screen reaction.
            expression_management(TOUCH_EXPRESSION)

            # Gemini generates the actual voice reaction.
            await self.session.send_realtime_input(
                text=(
                    "A physical touch was detected on your body. "
                    "React naturally with only a very short playful "
                    "pain reaction, such as 'Ouch!' or 'Ouch, hey!' "
                    "Do not explain anything and do not ask a question."
                )
            )

            await asyncio.sleep(TOUCH_EXPRESSION_TIME)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            print(f"⚠️ Touch → Gemini error: {exc}")

        finally:
            if not self.closed:
                expression_management("neutral")
                print("🎭 Touch expression finished → neutral")

            if callable(self.on_touch_end):
                try:
                    self.on_touch_end()
                except Exception as exc:
                    print(f"⚠️ Touch expression cleanup callback failed: {exc}")

    def stop(self):
        self.closed = True

        if self._touch_task is not None:
            try:
                self._touch_task.cancel()
            except Exception:
                pass
            self._touch_task = None

        if self.button is not None:
            try:
                self.button.when_pressed = None
            except Exception:
                pass

            try:
                self.button.close()
            except Exception:
                pass

            self.button = None

        print("👆 Touch sensor stopped.")
