#!/usr/bin/env python3
"""
ArduCam Camera Interface & Acquisition Test
Target: Raspberry Pi 4B payload computer, ArduCam USB (UVC) camera

Test steps
  1. USB detection        - camera powers on and is enumerated over USB
  2. Interface init       - Pi recognizes ArduCam as an active V4L2 video device
                            and software can open it and read frames
  3. Config + capture     - configure >= 960x540 @ 24 fps, capture ONLY after
                            trigger, record for the full experiment (500 s)  [PAY-12]
  4. Stop on trigger end  - camera ceases data transmission after the experiment

Usage examples
  python3 arducam_acquisition_test.py                     # full 500 s test, press Enter to trigger
  python3 arducam_acquisition_test.py --trigger immediate
  python3 arducam_acquisition_test.py --duration 20       # quick dry run (Step 3 marked as dry run)
  python3 arducam_acquisition_test.py --device /dev/video0 --no-save
  python3 arducam_acquisition_test.py --trigger gpio --gpio-pin 17

Dependencies
  sudo apt install v4l-utils usbutils python3-opencv
  (gpio trigger only) sudo apt install python3-gpiozero
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time

try:
    import cv2
except ImportError:
    sys.exit("OpenCV not found. Install with: sudo apt install python3-opencv")

# ---------------------------------------------------------------------------
# Requirements (PAY-12)
# ---------------------------------------------------------------------------
REQ_WIDTH = 960
REQ_HEIGHT = 540
REQ_FPS = 24.0
REQ_DURATION_S = 500.0
FPS_TOLERANCE = 0.05          # measured fps may be up to 5% below target
IDLE_SAMPLE_S = 5.0           # seconds to sample USB activity when idle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(cmd, timeout=10):
    """Run a shell command and return (returncode, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return -1, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, f"timeout: {' '.join(cmd)}"


def log(msg):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def find_arducam_video_device():
    """Parse `v4l2-ctl --list-devices` and return the first /dev/videoN under
    a block whose name mentions Arducam. Returns (device or None, raw output)."""
    rc, out = run(["v4l2-ctl", "--list-devices"])
    if rc != 0:
        return None, out
    current_is_arducam = False
    for line in out.splitlines():
        if not line.startswith(("\t", " ")):          # block header
            current_is_arducam = "arducam" in line.lower()
        elif current_is_arducam:
            dev = line.strip()
            if dev.startswith("/dev/video"):
                return dev, out
    return None, out


def processes_holding(device):
    """Return list of PIDs that have `device` open (scans /proc, no psmisc needed)."""
    target = os.path.realpath(device)
    holders = []
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = fd_dir.split("/")[2]
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.path.realpath(os.path.join(fd_dir, fd)) == target:
                        holders.append(int(pid))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return holders


def usb_irq_count():
    """Sum interrupt counts for the xHCI USB controller from /proc/interrupts.
    Used as a proxy for USB data traffic (isochronous video generates many IRQs)."""
    total = 0
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                if "xhci" in line.lower():
                    parts = line.split()
                    total += sum(int(p) for p in parts[1:] if p.isdigit())
    except OSError:
        return None
    return total


def usb_irq_rate(seconds):
    """Average xHCI interrupts per second over `seconds`."""
    a = usb_irq_count()
    if a is None:
        return None
    time.sleep(seconds)
    b = usb_irq_count()
    return (b - a) / seconds


def open_camera(device):
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    return cap if cap.isOpened() else None


def configure_camera(cap, width, height, fps):
    # MJPG is normally required for UVC cameras to hit higher res/fps over USB 2.0
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    f = float(cap.get(cv2.CAP_PROP_FPS))
    fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
    fourcc = "".join(chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4))
    return w, h, f, fourcc


def wait_for_trigger(mode, gpio_pin):
    if mode == "immediate":
        log("Trigger mode: immediate")
        return
    if mode == "enter":
        input("\n>>> Camera is idle. Press ENTER to trigger the experiment... ")
        return
    if mode == "gpio":
        try:
            from gpiozero import Button
        except ImportError:
            sys.exit("gpiozero not installed: sudo apt install python3-gpiozero")
        btn = Button(gpio_pin, pull_up=False)
        log(f"Waiting for rising edge on GPIO{gpio_pin}...")
        btn.wait_for_press()
        btn.close()


# ---------------------------------------------------------------------------
# Test steps
# ---------------------------------------------------------------------------
def step1_usb_detection(args, ctx):
    log("STEP 1: USB detection")
    rc, lsusb = run(["lsusb"])
    ctx["lsusb"] = lsusb
    usb_lines = [l for l in lsusb.splitlines() if "arducam" in l.lower()]
    if args.usb_id:
        usb_lines += [l for l in lsusb.splitlines() if args.usb_id.lower() in l.lower()]
    usb_lines = sorted(set(usb_lines))

    dev, v4l_list = find_arducam_video_device()
    ctx["v4l2_list"] = v4l_list
    if args.device:
        dev = args.device
    ctx["device"] = dev

    passed = bool(usb_lines) or (dev is not None and "arducam" in v4l_list.lower())
    details = {
        "usb_matches": usb_lines,
        "video_device": dev,
    }
    if not passed:
        details["hint"] = ("No ArduCam found on USB. Check cable/ribbon seating and "
                           "power, or pass --usb-id VVVV:PPPP / --device /dev/videoN.")
    for l in usb_lines:
        log(f"  USB: {l}")
    log(f"  Video device: {dev}")
    return passed, details


def step2_interface_init(args, ctx):
    log("STEP 2: Interface initialization")
    dev = ctx.get("device")
    if not dev or not os.path.exists(dev):
        return False, {"error": f"video device not present: {dev}"}

    rc, caps = run(["v4l2-ctl", "-d", dev, "--all"])
    is_capture = "video capture" in caps.lower()
    _, formats = run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    ctx["formats"] = formats

    cap = open_camera(dev)
    if cap is None:
        return False, {"error": "cv2.VideoCapture could not open device",
                       "v4l2_capture_capability": is_capture}

    good = 0
    for _ in range(10):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            good += 1
    cap.release()     # release so the camera is idle until the trigger

    passed = is_capture and good >= 5
    log(f"  V4L2 capture capability: {is_capture}, frames read: {good}/10")
    return passed, {"v4l2_capture_capability": is_capture,
                    "test_frames_read": f"{good}/10"}


def step3_config_and_capture(args, ctx):
    log("STEP 3: Configuration and triggered capture [PAY-12]")
    dev = ctx["device"]
    details = {}

    # --- 3a: configuration check -----------------------------------------
    cap = open_camera(dev)
    if cap is None:
        return False, {"error": "could not open camera for configuration"}
    w, h, f, fourcc = configure_camera(cap, args.width, args.height, args.fps)
    cap.release()
    config_ok = w >= REQ_WIDTH and h >= REQ_HEIGHT and f >= REQ_FPS
    details["configured"] = {"width": w, "height": h, "fps": f, "fourcc": fourcc}
    log(f"  Configured: {w}x{h} @ {f:.2f} fps ({fourcc}) -> {'OK' if config_ok else 'BELOW REQ'}")

    # --- 3b: camera idle before trigger ----------------------------------
    time.sleep(1.0)
    holders_pre = processes_holding(dev)
    ctx["irq_rate_idle_pre"] = usb_irq_rate(IDLE_SAMPLE_S)
    idle_before_ok = len(holders_pre) == 0
    details["pre_trigger"] = {"processes_holding_device": holders_pre,
                              "usb_irq_rate_per_s": ctx["irq_rate_idle_pre"]}
    log(f"  Pre-trigger: device holders={holders_pre}, "
        f"USB IRQ rate={ctx['irq_rate_idle_pre']}")

    # --- 3c: wait for trigger, then record --------------------------------
    wait_for_trigger(args.trigger, args.gpio_pin)
    trigger_time = time.time()
    log(f"  TRIGGERED - recording for {args.duration:.0f} s")

    cap = open_camera(dev)
    if cap is None:
        return False, {**details, "error": "could not open camera after trigger"}
    w, h, f, fourcc = configure_camera(cap, args.width, args.height, args.fps)

    writer = None
    video_path = None
    if not args.no_save:
        os.makedirs(args.output, exist_ok=True)
        video_path = os.path.join(args.output, f"capture_{ctx['run_id']}.avi")
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*"MJPG"),
                                 args.fps, (w, h))

    frames = 0
    read_errors = 0
    wrong_size = 0
    timestamps = []
    irq_start = usb_irq_count()
    start = time.monotonic()
    last_report = start
    while True:
        now = time.monotonic()
        if now - start >= args.duration:
            break
        ok, frame = cap.read()
        if not ok or frame is None:
            read_errors += 1
            if read_errors > 50 and frames == 0:
                break
            continue
        frames += 1
        timestamps.append(time.monotonic())
        fh, fw = frame.shape[:2]
        if fw < REQ_WIDTH or fh < REQ_HEIGHT:
            wrong_size += 1
        if writer is not None:
            writer.write(frame)
        if now - last_report >= 30:
            el = now - start
            log(f"    {el:6.0f}/{args.duration:.0f} s  frames={frames}  "
                f"avg fps={frames / el:.2f}")
            last_report = now
    elapsed = time.monotonic() - start
    irq_end = usb_irq_count()

    # --- trigger end: stop acquisition ------------------------------------
    cap.release()
    if writer is not None:
        writer.release()
    ctx["stop_time"] = time.time()
    ctx["cap_released_flag"] = not cap.isOpened()
    ctx["irq_rate_active"] = ((irq_end - irq_start) / elapsed
                              if irq_start is not None and elapsed > 0 else None)
    log(f"  Recording ended after {elapsed:.1f} s; camera released")

    # --- stats -------------------------------------------------------------
    measured_fps = frames / elapsed if elapsed > 0 else 0.0
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    max_gap = max(gaps) if gaps else None
    dropped_est = sum(max(0, round(g * args.fps) - 1) for g in gaps if g > 1.5 / args.fps)

    fps_ok = measured_fps >= REQ_FPS * (1 - FPS_TOLERANCE)
    duration_ok = elapsed >= REQ_DURATION_S - 0.5
    size_ok = frames > 0 and wrong_size == 0

    details["recording"] = {
        "trigger_epoch": trigger_time,
        "duration_s": round(elapsed, 2),
        "frames": frames,
        "measured_fps": round(measured_fps, 3),
        "expected_frames": int(args.fps * elapsed),
        "estimated_dropped_frames": dropped_est,
        "max_frame_gap_s": round(max_gap, 4) if max_gap else None,
        "read_errors": read_errors,
        "frames_below_min_resolution": wrong_size,
        "video_file": video_path,
    }
    details["checks"] = {
        "resolution_config_>=960x540@24": config_ok,
        "idle_before_trigger": idle_before_ok,
        "measured_fps_ok": fps_ok,
        "frame_resolution_ok": size_ok,
        "full_duration_500s": duration_ok,
    }
    if not duration_ok:
        details["note"] = (f"Duration {args.duration:.0f} s is shorter than the required "
                           f"{REQ_DURATION_S:.0f} s; Step 3 cannot pass (dry run).")
    log(f"  Frames={frames}  measured fps={measured_fps:.2f}  "
        f"est. dropped={dropped_est}")

    passed = all(details["checks"].values())
    return passed, details


def step4_stop_after_trigger(args, ctx):
    log("STEP 4: Confirm data transmission stops after experiment")
    dev = ctx["device"]
    time.sleep(1.0)
    holders = processes_holding(dev)
    post_rate = usb_irq_rate(IDLE_SAMPLE_S)
    pre_rate = ctx.get("irq_rate_idle_pre")
    active_rate = ctx.get("irq_rate_active")

    # USB activity should drop back to roughly the pre-trigger idle level
    if post_rate is not None and pre_rate is not None:
        traffic_ok = post_rate <= max(pre_rate * 1.5, pre_rate + 100)
    else:
        traffic_ok = None    # could not measure; rely on device-release checks

    released_ok = ctx.get("cap_released_flag", False) and len(holders) == 0
    passed = released_ok and (traffic_ok is not False)

    log(f"  Device holders after stop: {holders}")
    log(f"  USB IRQ/s  idle-before={pre_rate}  active={active_rate}  idle-after={post_rate}")
    return passed, {
        "capture_object_released": ctx.get("cap_released_flag"),
        "processes_holding_device": holders,
        "usb_irq_rate_idle_before": pre_rate,
        "usb_irq_rate_during_capture": active_rate,
        "usb_irq_rate_idle_after": post_rate,
        "usb_traffic_returned_to_idle": traffic_ok,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="ArduCam Interface & Acquisition Test")
    ap.add_argument("--device", help="video device, e.g. /dev/video0 (auto-detect if omitted)")
    ap.add_argument("--usb-id", help="USB VID:PID to match in lsusb if name lacks 'Arducam'")
    ap.add_argument("--width", type=int, default=REQ_WIDTH)
    ap.add_argument("--height", type=int, default=REQ_HEIGHT)
    ap.add_argument("--fps", type=float, default=REQ_FPS)
    ap.add_argument("--duration", type=float, default=REQ_DURATION_S,
                    help="recording duration in seconds (requirement: 500)")
    ap.add_argument("--trigger", choices=["enter", "immediate", "gpio"], default="enter")
    ap.add_argument("--gpio-pin", type=int, default=17)
    ap.add_argument("--output", default="arducam_test_results")
    ap.add_argument("--no-save", action="store_true", help="don't write the video file")
    args = ap.parse_args()

    ctx = {"run_id": dt.datetime.now().strftime("%Y%m%d_%H%M%S")}
    steps = [
        ("1", "USB detection", step1_usb_detection),
        ("2", "Interface initialization", step2_interface_init),
        ("3", "Config + triggered capture (PAY-12)", step3_config_and_capture),
        ("4", "Stops after trigger ends", step4_stop_after_trigger),
    ]
    results = []
    for num, name, fn in steps:
        prereq_failed = any(r["step"] in ("1", "2") and not r["passed"] for r in results)
        if prereq_failed and num in ("3", "4"):
            results.append({"step": num, "name": name, "passed": False,
                            "details": {"skipped": "prerequisite step failed"}})
            continue
        try:
            passed, details = fn(args, ctx)
        except KeyboardInterrupt:
            passed, details = False, {"error": "aborted by operator"}
        except Exception as e:  # keep going so the report is still written
            passed, details = False, {"error": f"{type(e).__name__}: {e}"}
        results.append({"step": num, "name": name, "passed": passed, "details": details})
        log(f"  => Step {num}: {'PASS' if passed else 'FAIL'}\n")

    # ---- report ------------------------------------------------------------
    os.makedirs(args.output, exist_ok=True)
    report = {
        "test": "ArduCam Camera Interface & Acquisition Test",
        "run_id": ctx["run_id"],
        "host": os.uname().nodename,
        "parameters": vars(args),
        "results": results,
        "overall_pass": all(r["passed"] for r in results),
        "diagnostics": {k: ctx.get(k) for k in ("lsusb", "v4l2_list", "formats")},
    }
    json_path = os.path.join(args.output, f"report_{ctx['run_id']}.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    lines = ["=" * 64,
             "ArduCam Camera Interface & Acquisition Test - Results",
             f"Run {ctx['run_id']} on {report['host']}",
             "=" * 64]
    for r in results:
        mark_p = "X" if r["passed"] else " "
        mark_f = " " if r["passed"] else "X"
        lines.append(f"Step {r['step']}: {r['name']:<40} Pass [{mark_p}]  Fail [{mark_f}]")
    lines.append("-" * 64)
    lines.append(f"OVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    lines.append(f"Full report: {json_path}")
    summary = "\n".join(lines)
    print("\n" + summary)
    with open(os.path.join(args.output, f"summary_{ctx['run_id']}.txt"), "w") as f:
        f.write(summary + "\n")

    sys.exit(0 if report["overall_pass"] else 1)


if __name__ == "__main__":
    main()
