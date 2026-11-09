from pathlib import Path
from tkinter import *
from PIL import Image, ImageTk


# Always resolve GIFs relative to this file so SHADOW works from VS Code,
# the project folder, or another current working directory.
BASE_DIR = Path(__file__).resolve().parent
GIF_DIR = BASE_DIR / "gifs2"

expression = "neutral"

window = None
label = None

frames = []
current_frame = 0
current_expression = None


def available_expressions():
    """Return the GIF names available in gifs2/ (without .gif)."""
    if not GIF_DIR.exists():
        return set()
    return {path.stem.lower() for path in GIF_DIR.glob("*.gif")}


def load_expression(exp):
    global frames
    global current_frame
    global current_expression

    exp = str(exp).strip().lower()
    current_expression = exp
    current_frame = 0
    frames.clear()

    gif_path = GIF_DIR / f"{exp}.gif"

    try:
        if not gif_path.exists():
            raise FileNotFoundError(f"GIF not found: {gif_path}")

        gif = Image.open(gif_path)

        try:
            while True:
                frame = gif.convert("RGB").copy()
                frame = frame.resize((480, 320), Image.Resampling.LANCZOS)
                frames.append(ImageTk.PhotoImage(frame))
                gif.seek(len(frames))
        except EOFError:
            pass
        finally:
            gif.close()

        print(f"🎭 Expression changed to: {exp}")

    except Exception as e:
        print(f"❌ Failed to load expression '{exp}': {e}")


def play_gif():
    global current_frame

    if frames and label is not None:
        label.config(image=frames[current_frame])
        current_frame += 1
        if current_frame >= len(frames):
            current_frame = 0

    if window is not None:
        window.after(100, play_gif)


def expression_window():
    global window
    global label

    window = Tk()
    window.title("Shadow GUI")
    window.geometry("480x320")
    window.configure(bg="black")
    window.attributes("-fullscreen", True)

    label = Label(window, bg="black")
    label.pack(expand=True)

    load_expression("neutral")
    play_gif()
    window.mainloop()


def expression_management(exp):
    """Request an expression change from any SHADOW thread."""
    global expression

    expression = str(exp).strip().lower()

    if window is not None:
        window.after(0, load_expression, expression)
