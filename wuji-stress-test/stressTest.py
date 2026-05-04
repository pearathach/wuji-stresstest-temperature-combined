#!/usr/bin/env python3
"""
Wuji Hand single-joint cycle stress test (real-time streamed trajectory).
Includes non-blocking telemetry and CSV logging.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import signal
import time
from typing import Optional, Tuple, Union

import numpy as np
import wujihandpy

FINGER_NAME_TO_INDEX = {
    "thumb": 0,
    "index": 1,
    "middle": 2,
    "ring": 3,
    "pinky": 4,
    "little": 4,
}


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
        try:
            joint.write_joint_enabled(False)
        except Exception:
            pass
        os._exit(1)
    return emergency_stop


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


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
) -> Tuple[bool, np.ndarray, float]:
    """
    Stream intermediate joint positions and poll telemetry at a fixed rate.
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
            # refresh cached hand telemetry (non-blocking)
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

            pos = eff = vbus = temp = float("nan")
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
                temp = hand.get_joint_temperature()[finger_idx, joint_idx]
            except Exception:
                pass
            try:
                err = int(hand.get_joint_error_code()[finger_idx, joint_idx])
            except Exception:
                pass

            print(
                f"tel f{finger_idx} j{joint_idx}: pos={pos:6.3f} rad | "
                f"effort={eff:6.3f} A | vbus={vbus:5.2f} V | temp={temp:4.1f} C | err={err}"
            )

            if csv_writer:
                csv_writer.writerow([time.time(), finger_idx, joint_idx, pos, eff, vbus, temp, err])

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
    ap = argparse.ArgumentParser(description="Wuji Hand Stress Test with Telemetry and CSV Logging.")
    ap.add_argument("finger", type=parse_finger, help="Finger index 0-4 or name.")
    ap.add_argument("joint", type=parse_joint, help="Joint index 0-3.")
    ap.add_argument("cycles", nargs="?", default=None, help="Cycles (int or 'inf').")
    
    ap.add_argument("--serial", type=str, default=None)
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

    ap.add_argument("--update-hz", type=float, default=100.0)
    ap.add_argument("--telemetry-hz", type=float, default=10.0)
    ap.add_argument("--write-to-csv", type=str, help="Path to save telemetry data (e.g., results.csv)")
    ap.add_argument("--rt-filter-hz", type=float, default=5.0)
    ap.add_argument("--enable-upstream", action="store_true", default=True)
    ap.add_argument("--end-wait", type=float, default=2.0)
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
            # Set all joints
            hand.write_joint_effort_limit(float(args.effort_limit))
            print(f"Set effort limit to {args.effort_limit} A for entire hand")
        else:
            # Set only the selected joint (keep others unchanged)
            cur = hand.read_joint_effort_limit()
            cur[args.finger, args.joint] = float(args.effort_limit)
            hand.write_joint_effort_limit(cur)
            print(f"Set effort limit to {args.effort_limit} A for finger {args.finger} joint {args.joint}")

    signal.signal(signal.SIGINT, make_emergency_stop(joint_obj))
    joint_obj.write_joint_enabled(True)

    csv_file = None
    csv_writer = None
    if args.write_to_csv:
        csv_file = open(args.write_to_csv, mode='w', newline='')
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["timestamp", "finger", "joint", "position_rad", "effort_a", "vbus_v", "temp_c", "error_code"])

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
                base_cmd, telemetry_period_s, next_tel, csv_writer
            )

            i = 0
            while i < cycles:
                for target_pos in [hi, lo]:
                    _, base_cmd, next_tel = stream_move_for_duration(
                        controller, hand, args.finger, args.joint,
                        base_cmd[args.finger, args.joint], target_pos, half, 
                        args.tol, args.update_hz, base_cmd, 
                        telemetry_period_s, next_tel, csv_writer
                    )
                i += 1
                print(f"--- Completed Cycle {i}/{cycles if math.isfinite(cycles) else 'inf'} ---")

            # Final return to zero
            stream_move_for_duration(
                controller, hand, args.finger, args.joint,
                lo, 0.0, args.end_wait, args.tol, args.update_hz,
                base_cmd, telemetry_period_s, next_tel, csv_writer
            )

    finally:
        if csv_file:
            csv_file.close()
        try:
            joint_obj.write_joint_enabled(False)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())