#!/usr/bin/env python3
"""
Combined Wuji Hand Stress Test with External Temperature Logging and Live Dashboard.
Combines stress test telemetry with MAX31856 thermocouple readings.
Includes WebSocket server for real-time browser dashboard.

WebSocket server runs on ws://localhost:8765 by default.
Install dependencies: pip install pyserial websockets numpy wujihandpy
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import queue
import signal
import threading
import time
import webbrowser
from datetime import datetime
from typing import Optional, Tuple, Union

import numpy as np
import serial
import serial.tools.list_ports
import websockets
import wujihandpy

FINGER_NAME_TO_INDEX = {
    "thumb": 0,
    "index": 1,
    "middle": 2,
    "ring": 3,
    "pinky": 4,
    "little": 4,
}

# Get directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PATH = os.path.join(SCRIPT_DIR, "stress_test_dashboard.html")

# Latest temperature readings from Arduino (shared between threads)
latest_temps = {
    "sensor1_tc": float("nan"),
    "sensor1_cj": float("nan"),
    "sensor2_tc": float("nan"),
    "sensor2_cj": float("nan"),
}
temps_lock = threading.Lock()
stop_temp_thread = threading.Event()

# WebSocket broadcast queue and connected clients
ws_broadcast_queue: queue.Queue = queue.Queue()
connected_clients: set = set()
stop_ws_server = threading.Event()


def parse_finger(value: str) -> int:
    v = value.strip().lower()
    if v.isdigit():
        idx = int(v)
    else:
        if v not in FINGER_NAME_TO_INDEX:
            raise argparse.ArgumentTypeError(
                f"Unknown finger '{value}'. Use 0-4 or one of: {', '.join(FINGER_NAME_TO_INDEX.keys())}"
            )
        idx = FINGER_NAME_TO_INDEX[v]
    if idx < 0 or idx > 4:
        raise argparse.ArgumentTypeError("Finger must be in range 0-4.")
    return idx


def parse_joint(value: str) -> int:
    v = value.strip().lower()
    if not v.isdigit():
        raise argparse.ArgumentTypeError("Joint must be an integer in range 0-3.")
    j = int(v)
    if j < 0 or j > 3:
        raise argparse.ArgumentTypeError("Joint must be in range 0-3.")
    return j


def parse_cycles(value: Optional[str]) -> Union[int, float]:
    if value is None:
        return math.inf
    v = value.strip().lower()
    if v in {"inf", "infinity", "+inf", "+infinity"}:
        return math.inf
    try:
        n = int(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError("cycles must be an integer, -1, or 'inf'") from e
    if n == -1:
        return math.inf
    if n < 0:
        raise argparse.ArgumentTypeError("cycles must be >= 0, or -1 for infinity.")
    return n


def clamp_limits(lower: float, upper: float, margin: float) -> Tuple[float, float]:
    lo = lower + margin
    hi = upper - margin
    if lo >= hi:
        lo, hi = lower, upper
    return lo, hi


def make_emergency_stop(joint: wujihandpy.Joint):
    def emergency_stop(signum, frame):
        stop_temp_thread.set()
        stop_ws_server.set()
        try:
            joint.write_joint_enabled(False)
        except Exception:
            pass
        os._exit(1)
    return emergency_stop


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def find_arduino_port() -> Optional[str]:
    """Auto-detect Arduino port."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if "Arduino" in port.description or "ACM" in port.device:
            return port.device
    return None


def parse_multi_sensor_line(line: str) -> dict:
    """
    Parse multi-sensor data format: S1:tcTemp:cjTemp,S2:tcTemp:cjTemp
    Returns dict with sensor readings.
    """
    readings = {}
    parts = line.split(",")
    for part in parts:
        try:
            sensor_data = part.split(":")
            if len(sensor_data) >= 3 and sensor_data[0].startswith("S"):
                sensor_num = int(sensor_data[0][1:])
                tc_temp = float(sensor_data[1])
                cj_temp = float(sensor_data[2])
                readings[f"sensor{sensor_num}_tc"] = round(tc_temp, 2)
                readings[f"sensor{sensor_num}_cj"] = round(cj_temp, 2)
        except (ValueError, IndexError):
            continue
    return readings


def temperature_reader_thread(port: str, baud: int = 115200):
    """Background thread that reads temperature from Arduino."""
    global latest_temps
    
    try:
        ser = serial.Serial(port, baud, timeout=1)
        time.sleep(2)  # Arduino reset delay
        print(f"[TEMP] Connected to Arduino on {port}")
        
        while not stop_temp_thread.is_set():
            try:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                
                # Skip info/error messages
                if line.startswith(("ERR", "FAULT", "MAX31856", "Init", "Sensor", "-")):
                    continue
                
                readings = parse_multi_sensor_line(line)
                if readings:
                    with temps_lock:
                        latest_temps.update(readings)
                        
            except Exception as e:
                if not stop_temp_thread.is_set():
                    print(f"[TEMP] Read error: {e}")
                time.sleep(0.1)
                
        ser.close()
        
    except serial.SerialException as e:
        print(f"[TEMP] Failed to open {port}: {e}")


def get_current_temps() -> dict:
    """Get the latest temperature readings thread-safely."""
    with temps_lock:
        return latest_temps.copy()


def to_json_safe(val):
    """Convert numpy types to Python native types for JSON serialization."""
    if val is None:
        return None
    if isinstance(val, (np.floating, np.float32, np.float64)):
        return float(val) if np.isfinite(val) else None
    if isinstance(val, (np.integer, np.int32, np.int64)):
        return int(val)
    if isinstance(val, float):
        return val if np.isfinite(val) else None
    return val


def broadcast_telemetry(data: dict):
    """Queue telemetry data for WebSocket broadcast."""
    try:
        ws_broadcast_queue.put_nowait(data)
    except queue.Full:
        pass  # Drop if queue is full


async def websocket_handler(websocket):
    """Handle WebSocket client connections."""
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"[WS] Client connected: {client_addr}")
    try:
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        print(f"[WS] Client disconnected: {client_addr}")


async def ws_broadcast_loop():
    """Continuously broadcast queued data to all WebSocket clients."""
    while not stop_ws_server.is_set():
        try:
            # Non-blocking check for data
            try:
                data = ws_broadcast_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            
            if connected_clients:
                message = json.dumps(data)
                await asyncio.gather(
                    *[client.send(message) for client in connected_clients],
                    return_exceptions=True
                )
        except Exception as e:
            print(f"[WS] Broadcast error: {e}")
            await asyncio.sleep(0.1)


async def run_websocket_server(host: str, port: int):
    """Run the WebSocket server."""
    print(f"[WS] Starting WebSocket server on ws://{host}:{port}")
    
    async with websockets.serve(websocket_handler, host, port):
        await ws_broadcast_loop()


def websocket_server_thread(host: str, port: int):
    """Run WebSocket server in a separate thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_websocket_server(host, port))
    except Exception as e:
        if not stop_ws_server.is_set():
            print(f"[WS] Server error: {e}")
    finally:
        loop.close()


def stream_move_for_duration(
    controller,
    hand: wujihandpy.Hand,
    finger_idx: int,
    joint_idx: int,
    start: float,
    target: float,
    duration_s: float,
    tol: float,
    update_hz: float,
    base_cmd: np.ndarray,
    telemetry_period_s: float,
    next_telemetry_time: float,
    csv_writer: Optional[csv.writer] = None,
    cycle: int = 0,
) -> Tuple[bool, np.ndarray, float]:
    """
    Stream intermediate joint positions and poll telemetry at a fixed rate.
    Now also logs external temperature sensor readings.
    """
    duration_s = max(duration_s, 0.0)
    update_hz = max(update_hz, 1.0)
    period = 1.0 / update_hz

    t0 = time.monotonic()
    t_end = t0 + duration_s
    reached = False
    tick = 0

    base_cmd[finger_idx, joint_idx] = float(start)

    while True:
        now = time.monotonic()
        if now >= t_end:
            break

        u = _clamp01((now - t0) / max(duration_s, 1e-9))
        base_cmd[finger_idx, joint_idx] = float(start + (target - start) * u)

        controller.set_joint_target_position(base_cmd)

        # --- Telemetry and Logging ---
        if telemetry_period_s > 0 and now >= next_telemetry_time:
            # Refresh cached hand telemetry (non-blocking)
            try:
                hand.read_joint_bus_voltage_unchecked()
            except Exception:
                pass
            try:
                hand.read_joint_temperature_unchecked()
            except Exception:
                pass
            try:
                hand.read_joint_error_code_unchecked()
            except Exception:
                pass
            try:
                hand.read_joint_effort_unchecked()
            except Exception:
                pass

            pos = eff = vbus = motor_temp = float("nan")
            err = -1

            try:
                pos = controller.get_joint_actual_position()[finger_idx, joint_idx]
            except Exception:
                pass
            try:
                eff = controller.get_joint_actual_effort()[finger_idx, joint_idx]
                if not np.isfinite(eff):
                    eff = float("nan")
            except Exception:
                pass
            if not np.isfinite(eff):
                try:
                    eff = hand.get_joint_effort()[finger_idx, joint_idx]
                except Exception:
                    pass
            try:
                vbus = hand.get_joint_bus_voltage()[finger_idx, joint_idx]
            except Exception:
                pass
            try:
                motor_temp = hand.get_joint_temperature()[finger_idx, joint_idx]
            except Exception:
                pass
            try:
                err = int(hand.get_joint_error_code()[finger_idx, joint_idx])
            except Exception:
                pass

            # Get external temperature readings
            ext_temps = get_current_temps()
            s1_tc = ext_temps.get("sensor1_tc", float("nan"))
            s1_cj = ext_temps.get("sensor1_cj", float("nan"))
            s2_tc = ext_temps.get("sensor2_tc", float("nan"))
            s2_cj = ext_temps.get("sensor2_cj", float("nan"))

            print(
                f"tel f{finger_idx} j{joint_idx}: pos={pos:6.3f} rad | "
                f"effort={eff:6.3f} A | vbus={vbus:5.2f} V | motor_temp={motor_temp:4.1f} C | "
                f"ext_temp1={s1_tc:5.1f} C | ext_temp2={s2_tc:5.1f} C | err={err}"
            )

            if csv_writer:
                csv_writer.writerow([
                    time.time(), 
                    cycle,
                    finger_idx, 
                    joint_idx, 
                    pos, 
                    eff, 
                    vbus, 
                    motor_temp, 
                    err,
                    s1_tc,
                    s1_cj,
                    s2_tc,
                    s2_cj,
                ])

            # Broadcast via WebSocket (temperature format for existing dashboard)
            broadcast_telemetry({
                "type": "temperature",
                "sensors": [
                    {
                        "sensor": 1,
                        "thermocouple_c": to_json_safe(s1_tc),
                        "thermocouple_f": to_json_safe(s1_tc * 9/5 + 32) if np.isfinite(s1_tc) else None,
                        "cold_junction_c": to_json_safe(s1_cj),
                    },
                    {
                        "sensor": 2,
                        "thermocouple_c": to_json_safe(s2_tc),
                        "thermocouple_f": to_json_safe(s2_tc * 9/5 + 32) if np.isfinite(s2_tc) else None,
                        "cold_junction_c": to_json_safe(s2_cj),
                    },
                ],
                "timestamp": datetime.now().isoformat(),
            })
            
            # Also broadcast stress test telemetry
            broadcast_telemetry({
                "type": "stress_test",
                "cycle": cycle,
                "finger": finger_idx,
                "joint": joint_idx,
                "position_rad": to_json_safe(pos),
                "effort_a": to_json_safe(eff),
                "vbus_v": to_json_safe(vbus),
                "motor_temp_c": to_json_safe(motor_temp),
                "error_code": to_json_safe(err),
                "ext_sensor1_tc_c": to_json_safe(s1_tc),
                "ext_sensor2_tc_c": to_json_safe(s2_tc),
                "timestamp": datetime.now().isoformat(),
            })

            missed = int((now - next_telemetry_time) // telemetry_period_s)
            next_telemetry_time += (missed + 1) * telemetry_period_s

        try:
            actual = controller.get_joint_actual_position()[finger_idx, joint_idx]
            if abs(actual - target) <= tol:
                reached = True
        except Exception:
            pass

        tick += 1
        next_tick_time = t0 + tick * period
        sleep_s = next_tick_time - time.monotonic()
        if sleep_s > 0:
            time.sleep(sleep_s)

    base_cmd[finger_idx, joint_idx] = float(target)
    try:
        controller.set_joint_target_position(base_cmd)
    except Exception:
        pass

    return reached, base_cmd, next_telemetry_time


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Wuji Hand Stress Test with External Temperature Logging."
    )
    ap.add_argument("finger", type=parse_finger, help="Finger index 0-4 or name.")
    ap.add_argument("joint", type=parse_joint, help="Joint index 0-3.")
    ap.add_argument("cycles", nargs="?", default=None, help="Cycles (int or 'inf').")
    
    ap.add_argument("--serial", type=str, default=None, help="Wuji Hand serial number")
    ap.add_argument("--margin", type=float, default=0.02)
    ap.add_argument("--tol", type=float, default=0.03)

    ap.add_argument(
        "--effort-limit",
        type=float,
        default=None,
        help="Set joint effort limit in A (0.0–3.5). If omitted, keep device default.",
    )
    ap.add_argument(
        "--effort-scope",
        choices=["joint", "hand"],
        default="joint",
        help="Apply effort limit to just the selected joint or the whole hand (default: joint).",
    )

    timing = ap.add_mutually_exclusive_group()
    timing.add_argument("--cycle-time", type=float, default=1.0)
    timing.add_argument("--speed", type=float)

    ap.add_argument("--update-hz", type=float, default=10.0)
    ap.add_argument("--telemetry-hz", type=float, default=10.0)
    ap.add_argument("--write-to-csv", type=str, help="Path to save telemetry data (e.g., results.csv)")
    ap.add_argument("--rt-filter-hz", type=float, default=5.0)
    ap.add_argument("--enable-upstream", action="store_true", default=True)
    ap.add_argument("--end-wait", type=float, default=2.0)
    
    # Arduino temperature sensor arguments
    ap.add_argument(
        "--arduino-port",
        type=str,
        default=None,
        help="Serial port for Arduino temperature sensor (auto-detect if not specified)",
    )
    ap.add_argument(
        "--arduino-baud",
        type=int,
        default=115200,
        help="Baud rate for Arduino serial (default: 115200)",
    )
    ap.add_argument(
        "--no-temp-sensor",
        action="store_true",
        help="Disable external temperature sensor reading",
    )
    
    # WebSocket / Dashboard arguments
    ap.add_argument(
        "--ws-host",
        type=str,
        default="0.0.0.0",
        help="WebSocket server host (default: 0.0.0.0)",
    )
    ap.add_argument(
        "--ws-port",
        type=int,
        default=8765,
        help="WebSocket server port (default: 8765)",
    )
    ap.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable WebSocket server and browser dashboard",
    )
    ap.add_argument(
        "--no-browser",
        action="store_true",
        help="Start WebSocket server but don't open browser automatically",
    )
    
    args = ap.parse_args()

    # Validations
    if args.speed is not None:
        args.cycle_time = 1.0 / args.speed
    if args.cycle_time <= 0 or args.update_hz <= 0 or args.telemetry_hz < 0:
        ap.error("Timing values must be positive (telemetry-hz may be 0 to disable).")

    cycles = parse_cycles(args.cycles)
    hand = wujihandpy.Hand(serial_number=args.serial) if args.serial else wujihandpy.Hand()
    joint_obj = hand.finger(args.finger).joint(args.joint)

    # Validate and apply effort limit
    if args.effort_limit is not None:
        if args.effort_limit < 0.0 or args.effort_limit > 3.5:
            ap.error("--effort-limit must be in [0.0, 3.5] A")

        if args.effort_scope == "hand":
            hand.write_joint_effort_limit(float(args.effort_limit))
            print(f"Set effort limit to {args.effort_limit} A for entire hand")
        else:
            cur = hand.read_joint_effort_limit()
            cur[args.finger, args.joint] = float(args.effort_limit)
            hand.write_joint_effort_limit(cur)
            print(f"Set effort limit to {args.effort_limit} A for finger {args.finger} joint {args.joint}")

    signal.signal(signal.SIGINT, make_emergency_stop(joint_obj))
    joint_obj.write_joint_enabled(True)

    # Start temperature reading thread
    temp_thread = None
    if not args.no_temp_sensor:
        arduino_port = args.arduino_port
        if arduino_port is None:
            arduino_port = find_arduino_port()
        
        if arduino_port:
            temp_thread = threading.Thread(
                target=temperature_reader_thread,
                args=(arduino_port, args.arduino_baud),
                daemon=True
            )
            temp_thread.start()
            time.sleep(2.5)  # Allow time for Arduino connection
        else:
            print("[TEMP] No Arduino detected. Running without external temperature logging.")

    # Start WebSocket server for live dashboard
    ws_thread = None
    if not args.no_dashboard:
        ws_thread = threading.Thread(
            target=websocket_server_thread,
            args=(args.ws_host, args.ws_port),
            daemon=True
        )
        ws_thread.start()
        time.sleep(0.5)  # Allow server to start
        
        # Open dashboard in browser
        if not args.no_browser:
            # Use existing temperature_dashboard.html (it connects to same WebSocket)
            dashboard_file = os.path.join(SCRIPT_DIR, "temperature_dashboard.html")
            if os.path.exists(dashboard_file):
                dashboard_url = f"file://{dashboard_file}"
                print(f"[Browser] Opening dashboard: {dashboard_url}")
                webbrowser.open(dashboard_url)
            else:
                print(f"[Browser] Dashboard not found: {dashboard_file}")

    csv_file = None
    csv_writer = None
    if args.write_to_csv:
        csv_file = open(args.write_to_csv, mode='w', newline='')
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow([
            "timestamp", 
            "cycle",
            "finger", 
            "joint", 
            "position_rad", 
            "effort_a", 
            "vbus_v", 
            "motor_temp_c", 
            "error_code",
            "ext_sensor1_tc_c",
            "ext_sensor1_cj_c",
            "ext_sensor2_tc_c",
            "ext_sensor2_cj_c",
        ])

    try:
        lower = joint_obj.read_joint_lower_limit()
        upper = joint_obj.read_joint_upper_limit()
        lo, hi = clamp_limits(lower, upper, args.margin)
        half = args.cycle_time / 2.0

        with hand.realtime_controller(
            enable_upstream=bool(args.enable_upstream),
            filter=wujihandpy.filter.LowPass(cutoff_freq=float(args.rt_filter_hz)),
        ) as controller:
            # Warm-up: allow upstream caches to populate
            t_warm = time.monotonic() + 0.5
            while time.monotonic() < t_warm:
                try:
                    _ = controller.get_joint_actual_position()
                    _ = controller.get_joint_actual_effort()
                except Exception:
                    pass
                time.sleep(0.01)

            # Quick diagnostic
            try:
                eff_arr = controller.get_joint_actual_effort()
                finite = np.isfinite(eff_arr)
                print(f"[diag] effort finite count: {finite.sum()}/{eff_arr.size}")
            except Exception as e:
                print(f"[diag] effort read failed: {type(e).__name__}: {e}")

            try:
                base_cmd = controller.get_joint_actual_position().astype(np.float64, copy=True)
                start_pos = base_cmd[args.finger, args.joint]
            except Exception:
                base_cmd = np.zeros((5, 4), dtype=np.float64)
                start_pos = 0.0

            telemetry_period_s = 0.0 if args.telemetry_hz <= 0 else 1.0 / args.telemetry_hz
            next_tel = time.monotonic()

            # Pre-flight move to lo
            _, base_cmd, next_tel = stream_move_for_duration(
                controller, hand, args.finger, args.joint,
                start_pos, lo, half, args.tol, args.update_hz, 
                base_cmd, telemetry_period_s, next_tel, csv_writer, cycle=0
            )

            i = 0
            while i < cycles:
                for target_pos in [hi, lo]:
                    _, base_cmd, next_tel = stream_move_for_duration(
                        controller, hand, args.finger, args.joint,
                        base_cmd[args.finger, args.joint], target_pos, half, 
                        args.tol, args.update_hz, base_cmd, 
                        telemetry_period_s, next_tel, csv_writer, cycle=i+1
                    )
                i += 1
                print(f"--- Completed Cycle {i}/{cycles if math.isfinite(cycles) else 'inf'} ---")

            # Final return to zero
            stream_move_for_duration(
                controller, hand, args.finger, args.joint,
                lo, 0.0, args.end_wait, args.tol, args.update_hz,
                base_cmd, telemetry_period_s, next_tel, csv_writer, cycle=i
            )

    finally:
        stop_temp_thread.set()
        stop_ws_server.set()
        if csv_file:
            csv_file.close()
        try:
            joint_obj.write_joint_enabled(False)
        except Exception:
            pass
        if temp_thread and temp_thread.is_alive():
            temp_thread.join(timeout=1.0)
        if ws_thread and ws_thread.is_alive():
            ws_thread.join(timeout=1.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
