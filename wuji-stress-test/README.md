# Wuji Hand – Single Joint Stress Test

This script performs a constant-time stress test on any single joint of a Wuji Hand finger by repeatedly cycling it between its calibrated joint limits.

It is designed for durability/lifetime testing and includes a hard software e-stop.

## Requirements

- **Linux (required)**
- **Python ≥ 3.8**
- **Wuji Hand SDK:**
  ```sh
  pip install wujihandpy
  ```
- **USB permissions configured**
- **Member of the `dialout` group**
- **Device visible as `/dev/ttyACM*`**

## Usage

```sh
python stressTest.py finger joint [cycles] [options]
```

Run help at any time:

```sh
python stressTest.py -h
```

### Output

```
usage: stressTest.py [-h] [--serial SERIAL] [--margin MARGIN] [--tol TOL]
                     [--cycle-time CYCLE_TIME | --speed SPEED] [--update-hz UPDATE_HZ]
                     [--telemetry-hz TELEMETRY_HZ] [--write-to-csv WRITE_TO_CSV]
                     [--rt-filter-hz RT_FILTER_HZ] [--enable-upstream]
                     [--end-wait END_WAIT] [--effort-limit EFFORT_LIMIT]
                     [--effort-scope {joint,hand}]
                     finger joint [cycles]

Wuji Hand: constant-time cycling of a chosen joint.
```

#### Positional Arguments

| Argument | Description |
|----------|-------------|
| finger   | Finger index 0–4 or name (thumb, index, middle, ring, pinky) |
| joint    | Joint index 0–3 within the selected finger |
| cycles   | Number of cycles. Use `inf` or `-1` for infinite (default: infinite) |

## Finger & Joint Mapping (IMPORTANT)

### Non-thumb fingers

(index, middle, ring, pinky)

| Joint | Meaning                        |
|-------|---------------------------------|
| 0     | MCP flexion (base joint)        |
| 1     | Side-to-side (abduction/adduction) |
| 2     | Middle joint                    |
| 3     | Last joint (DIP)                |

- Joint 1 is side-to-side
- Joints 0, 2, and 3 are the three flexion joints along the finger
- MCP = joint 0, middle = joint 2, last = joint 3

### Thumb

| Joint | Meaning                        |
|-------|---------------------------------|
| 0     | Thumb into the palm             |
| 1     | Thumb up toward the index finger|
| 2     | Middle thumb joint              |
| 3     | Last thumb joint                |

## Examples

Cycle index finger MCP joint (joint 0) for 100 cycles:
```sh
python stressTest.py index 0 100
```

Stress test pinky last joint forever:
```sh
python stressTest.py pinky 3 inf
```

Cycle thumb into-palm joint:
```sh
python stressTest.py thumb 0 50
```

Select a specific device (recommended):
```sh
python stressTest.py index 3 100 --serial 355537533533
```

Set effort limit to 2.0 A for just the selected joint:
```sh
python stressTest.py index 0 100 --effort-limit 2.0
```

Set effort limit to 1.5 A for the entire hand:
```sh
python stressTest.py index 0 100 --effort-limit 1.5 --effort-scope hand
```

## Effort Limiting

You can optionally set a joint effort (current) limit at startup:

```sh
--effort-limit 2.0
```

- Valid range: **0.0–3.5 A**
- Default: device default (not modified by script)

By default, only the selected joint's limit is changed. To apply the same limit to all joints:

```sh
--effort-scope hand
```

This is useful for durability testing at reduced torque or for thermal management.

## Timing Behavior

- Each cycle takes exactly the same amount of time
- A cycle = lower limit → upper limit → lower limit
- Cycle timing is enforced even if:
  - the joint reaches early
  - the joint reaches late
- This avoids timing jitter and uneven pauses
- **Default:** 1.0 second per cycle

You can set timing by either:

--cycle-time SECONDS (seconds per full cycle), or

--speed HZ (cycles per second), where cycle_time = 1/speed

Movement is paced by streaming setpoints in real-time (not "jump then wait").

Change with:
```sh
--cycle-time 0.5
```

Or:
```sh
--speed 2
```

## Telemetry Logging & CSV Output

The script can periodically read live telemetry and print a compact status line while running.
This includes:
- position (rad)
- effort (A)
- bus voltage (V)
- temperature (C)
- error code

Enable CSV logging with:
```sh
--write-to-csv results.csv
```

CSV columns are:
`timestamp, finger, joint, position_rad, effort_a, vbus_v, temp_c, error_code`

To disable telemetry (and therefore CSV writes), set:
```sh
--telemetry-hz 0
```

## Safety / E-Stop

- **Ctrl-C = immediate software e-stop**
  - Joint is disabled instantly
  - Process exits immediately (`os._exit`)


## End Behavior

- **For finite runs:**
  - Joint is commanded back to 0.0 radians
  - Script waits briefly
  - Joint is disabled cleanly
- **For infinite runs:**
  - Ctrl-C stops motion immediately
  - No return-to-zero motion
  