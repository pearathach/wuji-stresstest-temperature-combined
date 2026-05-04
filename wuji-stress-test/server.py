import serial
import json
import time
import threading
from flask import Flask, jsonify

PORT = "/dev/ttyACM2"
BAUD = 115200
NUM_SENSORS = 3

try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
except Exception as e:
    print(f"Warning: could not open serial port {PORT}: {e}")
    ser = None

if ser:
    # give device time to reset
    time.sleep(2)

app = Flask(__name__)

latest_data = {
    "time": 0,
    "temps_f": [None] * NUM_SENSORS,
}

start_time = time.time()


@app.route("/data")
def data():
    return jsonify(latest_data)


def serial_reader():
    global latest_data, ser
    if ser is None:
        return
    while True:
        try:
            line = ser.readline().decode("utf-8", errors="replace").strip()
        except Exception as e:
            print("Serial read error:", e)
            time.sleep(1)
            continue

        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print("JSON decode failed:", line)
            continue

        # accept either {'temps': [...]} or a plain list [..]
        if isinstance(msg, dict):
            temps_c = msg.get("temps")
        elif isinstance(msg, list):
            temps_c = msg
        elif isinstance(msg, (int, float)):
            # single numeric reading -> single-sensor list
            temps_c = [msg]
        else:
            print("Unexpected JSON message type:", type(msg), msg)
            continue
        if not isinstance(temps_c, list):
            print("Invalid temps field:", temps_c)
            continue

        temps_f = []
        for t in temps_c:
            try:
                if t is None:
                    temps_f.append(None)
                else:
                    temp_c = float(t)
                    temps_f.append(round(temp_c * 9 / 5 + 32, 2))
            except (ValueError, TypeError):
                temps_f.append(None)

        if len(temps_f) < NUM_SENSORS:
            temps_f.extend([None] * (NUM_SENSORS - len(temps_f)))
        else:
            temps_f = temps_f[:NUM_SENSORS]

        latest_data = {
            "time": round(time.time() - start_time, 2),
            "temps_f": temps_f,
        }


if __name__ == "__main__":
    threading.Thread(target=serial_reader, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)