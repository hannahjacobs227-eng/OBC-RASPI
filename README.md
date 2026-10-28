# Arducam-Raspi-verification test
Test procedure to verify Arducam

# To run:
bash
sudo apt install v4l-utils usbutils python3-opencv
python3 arducam_acquisition_test.py                  # full test, press Enter to trigger
python3 arducam_acquisition_test.py --duration 20    # quick dry run (Step 3 will show FAIL on duration)

# ArduCam Camera Interface & Acquisition Test

A test script (`arducam_acquisition_test.py`) that checks an ArduCam USB camera connected to the Raspberry Pi 4B payload computer. It runs the four steps of the Camera Interface & Acquisition Test procedure, including requirement **PAY-12**, and records Pass/Fail for each.

## What it tests

| Step | Check | Pass criteria |
|------|-------|---------------|
| 1 | Camera is powered on and detected over USB | ArduCam appears in `lsusb` and/or `v4l2-ctl --list-devices` |
| 2 | Pi recognizes the camera as an active video device | Device supports video capture, opens in software, and returns at least 5 of 10 test frames |
| 3 | Configuration and triggered capture (PAY-12) | Configured to at least 960×540 @ 24 fps; camera idle before the trigger; records for the full 500 s; measured frame rate at least 22.8 fps (5% tolerance); every frame at least 960×540 |
| 4 | Data stops once the experiment ends | Camera is released, no process holds the device, and USB activity returns to the idle level measured before the trigger |

If Step 1 or Step 2 fails, Steps 3 and 4 are skipped and marked as failed.

## Requirements

- Raspberry Pi 4B running Raspberry Pi OS
- ArduCam **USB (UVC)** camera
- Python 3 with OpenCV, plus V4L2 and USB tools:

```bash
sudo apt install v4l-utils usbutils python3-opencv
```

- GPIO trigger only:

```bash
sudo apt install python3-gpiozero
```

> **Note:** This script is written for USB ArduCam models. Ribbon-cable (CSI) models connect to the Pi's camera port, do not appear in `lsusb`, and need a different approach (`picamera2`).

## Running the test

1. Connect the ArduCam to the Pi.
2. Run the full test:

```bash
python3 arducam_acquisition_test.py
```

3. After Steps 1 and 2, the script pauses with the camera idle. Press **Enter** to trigger the experiment.
4. Recording runs for 500 s, with progress printed every 30 s.
5. Step 4 runs automatically when recording ends, and the results sheet is printed.

The full test takes about 9 minutes.

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--device /dev/videoN` | auto-detect | Use a specific video device |
| `--usb-id VVVV:PPPP` | none | Match the camera by USB ID if its name doesn't include "Arducam" |
| `--width`, `--height` | 960, 540 | Resolution to configure |
| `--fps` | 24 | Frame rate to configure |
| `--duration` | 500 | Recording length in seconds |
| `--trigger` | `enter` | `enter` (keypress), `immediate`, or `gpio` |
| `--gpio-pin` | 17 | BCM pin to wait on for a rising edge with `--trigger gpio` |
| `--output` | `arducam_test_results` | Folder for reports and video |
| `--no-save` | off | Don't write the video file |

### Examples

```bash
# Quick dry run to check the setup (Step 3 will fail on duration)
python3 arducam_acquisition_test.py --duration 20 --trigger immediate

# Trigger from the experiment controller on GPIO 17
python3 arducam_acquisition_test.py --trigger gpio --gpio-pin 17

# Specific device, no video saved
python3 arducam_acquisition_test.py --device /dev/video0 --no-save
```

## Output

Files are saved in `arducam_test_results/`, named by run time (`YYYYMMDD_HHMMSS`):

- `summary_<run>.txt`: the Pass/Fail sheet
- `report_<run>.json`: full measurements for each step, plus `lsusb`, device list and supported formats
- `capture_<run>.avi`: the recorded video (MJPG), unless `--no-save` is used

Example summary:

```
================================================================
ArduCam Camera Interface & Acquisition Test - Results
Run 20261028_145200 on payload-pi
================================================================
Step 1: USB detection                            Pass [X]  Fail [ ]
Step 2: Interface initialization                 Pass [X]  Fail [ ]
Step 3: Config + triggered capture (PAY-12)      Pass [X]  Fail [ ]
Step 4: Stops after trigger ends                 Pass [X]  Fail [ ]
----------------------------------------------------------------
OVERALL: PASS
```

The script exits with code `0` if every step passes and `1` otherwise, so it can be used in automated checks.

## How Step 4 measures "no data"

A USB camera that is streaming video generates a high rate of USB controller interrupts. The script counts these from `/proc/interrupts` in three windows: idle before the trigger, during recording, and idle after recording. Step 4 passes when the after-recording rate is back near the before-trigger level and no process still has the camera open. All three rates are saved in the JSON report.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| Step 1 fails | Reseat the cable, try another USB port, run `lsusb` to find the camera's ID and pass it with `--usb-id` |
| Wrong video device chosen | Run `v4l2-ctl --list-devices` and pass the right one with `--device` |
| Configured resolution or fps below requirement | Run `v4l2-ctl -d /dev/videoN --list-formats-ext` to see supported modes; 24 fps at 960×540 usually needs MJPG |
| Measured fps too low | Close other programs, try `--no-save` to see if video writing is the bottleneck, use a USB 3.0 port |
| Step 4 USB activity check fails | Make sure no other camera app is running; other USB devices can add background activity |
| `Permission denied` on the device | Add your user to the `video` group: `sudo usermod -aG video $USER`, then log out and back in |
