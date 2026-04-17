import time
import threading
from pynput import mouse, keyboard
import winsound

activity_count = 0
lock = threading.Lock()

def on_click(x, y, button, pressed):
    global activity_count
    if pressed:
        with lock:
            activity_count += 1

def on_press(key):
    global activity_count
    with lock:
        activity_count += 1

def monitor():
    with mouse.Listener(on_click=on_click) as m_listener:
        with keyboard.Listener(on_press=on_press) as k_listener:
            m_listener.join()
            k_listener.join()

# Start monitoring in background
t = threading.Thread(target=monitor, daemon=True)
t.start()

print("Monitoring input for 5 seconds... Try moving mouse and typing.")

for _ in range(5):
    time.sleep(1)
    with lock:
        print(f"Current activity count: {activity_count}")
        activity_count = 0

print("Testing winsound.Beep (as a proxy for typing sound)...")
winsound.Beep(1000, 100) # 1000Hz, 100ms
