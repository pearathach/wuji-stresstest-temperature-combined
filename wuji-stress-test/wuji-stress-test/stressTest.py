#!/usr/bin/env python3
"""
Wuji Hand single-joint cycle stress test.

- Select a finger (0-4 or name).
- Select a joint index (0-3).
- Runs cycles by moving between the joint's lower/upper limits.
- cycles = -1 or "inf" => run forever (default: forever).

Timing:
- Each cycle is forced to take exactly --cycle-time seconds (default: 1.0).
  (A cycle = lo -> hi -> lo, split into two equal half-cycles.)
- If the joint reaches early, we wait the remaining time.
- If it doesn't reach within the allotted time, we move on anyway (still constant time).

E-stop behavior:
- Ctrl-C (SIGINT) disables the joint and exits immediately via os._exit(1).

End behavior:
- On normal completion (finite cycles), command joint back to 0.0 rad and wait briefly,
  then disable the joint.
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import time
from typing import Optional, Tuple, Union

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
        return math.inf  # default: run forever
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
        os._exit(1)  # hard stop

    return emergency_stop


def move_for_duration(
    joint: wujihandpy.Joint,
    target: float,
    duration_s: float,
    tol: float,
    poll_hz: float,
) -> bool:
    """
    Command target immediately, then spend exactly duration_s seconds in this segment.
    Returns True if target was reached within tol at any point during the segment.
    """
    t_end = time.monotonic() + max(duration_s, 0.0)
    period = 1.0 / max(poll_hz, 1.0)

    joint.write_joint_target_position(target)

    reached = False
    while True:
        now = time.monotonic()
        if now >= t_end:
            break

        try:
            pos = joint.read_joint_actual_position()
            if abs(pos - target) <= tol:
                reached = True
        except Exception:
            # If reads fail transiently, keep timing deterministic
            pass

        remaining = t_end - now
        time.sleep(min(period, remaining))

    return reached


def main() -> int:
    ap = argparse.ArgumentParser(description="Wuji Hand: constant-time cycling of a chosen joint.")
    ap.add_argument(
        "finger",
        type=parse_finger,
        help="Finger index 0-4, or name: thumb/index/middle/ring/pinky",
    )
    ap.add_argument(
        "joint",
        type=parse_joint,
        help="Joint index 0-3 (within the selected finger)",
    )
    ap.add_argument(
        "cycles",
        nargs="?",
        default=None,
        help="Number of cycles. Use -1 or 'inf' for infinity. Default: infinity.",
    )
    ap.add_argument(
        "--serial",
        type=str,
        default=None,
        help="Optional device serial number to select a specific hand.",
    )
    ap.add_argument(
        "--margin",
        type=float,
        default=0.02,
        help="Radians to stay away from calibrated limits (default: 0.02). Set 0 for full range.",
    )
    ap.add_argument(
        "--tol",
        type=float,
        default=0.03,
        help="Position tolerance (radians) to consider target reached (default: 0.03).",
    )
    ap.add_argument(
        "--poll-hz",
        type=float,
        default=50.0,
        help="Polling rate while reading actual position (default: 50 Hz).",
    )
    ap.add_argument(
        "--cycle-time",
        type=float,
        default=1.0,
        help="Seconds per full cycle (lo->hi->lo). Enforced constant time (default: 1.0).",
    )
    ap.add_argument(
        "--end-wait",
        type=float,
        default=2.0,
        help="Seconds to allow the joint to return to 0.0 at the end (default: 2.0).",
    )
    args = ap.parse_args()

    cycles = parse_cycles(args.cycles)
    finger_idx = args.finger
    joint_idx = args.joint

    # Init hand/device
    if args.serial:
        hand = wujihandpy.Hand(serial_number=args.serial)
    else:
        hand = wujihandpy.Hand()

    joint = hand.finger(finger_idx).joint(joint_idx)

    # HARD e-stop
    handler = make_emergency_stop(joint)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    # Enable only this joint
    joint.write_joint_enabled(True)

    try:
        lower = joint.read_joint_lower_limit()
        upper = joint.read_joint_upper_limit()
        lo, hi = clamp_limits(lower, upper, args.margin)

        print(f"Finger {finger_idx}, joint {joint_idx}")
        print(f"Limits: lower={lower:.4f} rad, upper={upper:.4f} rad")
        if (lo, hi) != (lower, upper):
            print(f"Using:  lo={lo:.4f} rad, hi={hi:.4f} rad (margin={args.margin:.4f})")
        print(f"Cycle time: {args.cycle_time:.3f}s (constant)")

        half = max(args.cycle_time / 2.0, 0.0)

        # Establish known phase: spend exactly half-cycle moving to lo
        move_for_duration(joint, lo, half, args.tol, args.poll_hz)

        i = 0
        while i < cycles:
            ok_hi = move_for_duration(joint, hi, half, args.tol, args.poll_hz)
            ok_lo = move_for_duration(joint, lo, half, args.tol, args.poll_hz)

            i += 1
            if math.isfinite(cycles):
                print(f"cycle {i}/{int(cycles)} (reached_hi={ok_hi}, reached_lo={ok_lo})")
            else:
                print(f"cycle {i} (reached_hi={ok_hi}, reached_lo={ok_lo})")

    finally:
        # On normal completion: return to 0.0 position, then disable.
        # (Ctrl-C bypasses finally via os._exit in the handler.)
        try:
            joint.write_joint_target_position(0.0)
            t_end = time.monotonic() + max(args.end_wait, 0.0)
            while time.monotonic() < t_end:
                try:
                    pos = joint.read_joint_actual_position()
                    if abs(pos - 0.0) <= args.tol:
                        break
                except Exception:
                    pass
                time.sleep(0.02)
        except Exception:
            pass

        try:
            joint.write_joint_enabled(False)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
