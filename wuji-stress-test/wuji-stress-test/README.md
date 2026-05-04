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
                     [--poll-hz POLL_HZ] [--cycle-time CYCLE_TIME]
                     [--end-wait END_WAIT]
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

## Timing Behavior

- Each cycle takes exactly the same amount of time
- A cycle = lower limit → upper limit → lower limit
- Cycle timing is enforced even if:
  - the joint reaches early
  - the joint reaches late
- This avoids timing jitter and uneven pauses
- **Default:** 1.0 second per cycle

Change with:
```sh
--cycle-time 0.5
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
  