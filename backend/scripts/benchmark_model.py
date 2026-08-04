"""
Benchmark a YOLO weight file's inference speed (and rough detection stats) on
a single representative frame, repeated N times, to compare candidate models
(yolo11l/m/s/n, yolov8l/m/s/n, ...) before picking one for deployment.

Captures ONE real frame from a live camera (same HLSStreamReader used in
production) so the benchmark reflects real image content, then times pure
model inference in a tight loop — network/stream jitter is not included.

Usage (run from repo root, venv activated):

    python -m backend.scripts.benchmark_model --model yolo11nbest.pt
    python -m backend.scripts.benchmark_model --model yolo11lbest_finetuned.pt --imgsz 960

On the target Oracle VM (Linux/aarch64), restrict to the VM's 2 OCPU so
numbers are representative of production, not the dev machine's full core
count. Two equivalent ways:

    taskset -c 0,1 python -m backend.scripts.benchmark_model --model yolo11nbest.pt
    python -m backend.scripts.benchmark_model --model yolo11nbest.pt --cpus 2

(--cpus uses os.sched_setaffinity internally; only works on Linux. On
Windows/macOS it's a no-op with a warning — fine for relative comparisons
between models, but absolute ms won't match the ARM VM.)

No live camera reachable (offline dev)? Use --synthetic to benchmark on a
random noise frame, or --image path/to/frame.jpg for a saved real frame.
Results append to backend/logs/model_benchmark.csv for cross-model comparison.
"""
import argparse
import csv
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_CSV_PATH = os.path.join(_LOG_DIR, "model_benchmark.csv")
_CAMERAS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "cameras.json"
)

_CSV_COLUMNS = [
    "timestamp", "model_path", "imgsz", "cpus_pinned", "platform",
    "frame_source", "iterations", "avg_ms", "std_ms", "min_ms", "max_ms", "fps",
    "avg_detections_per_frame", "avg_confidence", "per_class_counts",
]


def _restrict_cpus(n: int) -> bool:
    """Pin this process to the first `n` CPU cores. Linux-only; returns
    whether it actually took effect (so the CSV row reflects reality)."""
    if not hasattr(os, "sched_setaffinity"):
        print(f"[warn] os.sched_setaffinity not available on {platform.system()} "
              f"— --cpus={n} ignored, results won't reflect a core-limited VM.")
        return False
    try:
        os.sched_setaffinity(0, set(range(n)))
        return True
    except OSError as exc:
        print(f"[warn] failed to set CPU affinity: {exc}")
        return False


def _grab_live_frame(camera_id: int):
    from backend.stream.hls_reader import HLSStreamReader

    with open(_CAMERAS_FILE, encoding="utf-8") as f:
        cameras = json.load(f)
    camera = next((c for c in cameras if c["id"] == camera_id), None)
    if camera is None:
        raise ValueError(f"camera_id={camera_id} not found in cameras.json")

    print(f"Connecting to camera {camera_id} ({camera['name']}) for a live sample frame...")
    reader = HLSStreamReader(camera["master_url"], fps_limit=1.0)
    frame = None
    try:
        for f in reader.stream_frames():
            frame = f
            reader.stop()
            break
    finally:
        reader.stop()
    if frame is None:
        raise RuntimeError(f"Could not grab a frame from camera {camera_id}")
    return frame, f"camera:{camera_id}"


def _load_frame(args):
    if args.synthetic:
        h, w = 720, 1280
        rng = np.random.default_rng(0)
        return rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8), "synthetic"
    if args.image:
        import cv2
        frame = cv2.imread(args.image)
        if frame is None:
            raise ValueError(f"Could not read image: {args.image}")
        return frame, f"image:{args.image}"
    return _grab_live_frame(args.camera_id)


def _percent_confidence_stats(results, target_classes):
    confidences = []
    class_counts: dict[str, int] = {}
    for result in results:
        if result.boxes is None:
            continue
        for box in result.boxes:
            class_name = result.names.get(int(box.cls[0]), "")
            if class_name not in target_classes:
                continue
            confidences.append(float(box.conf[0]))
            class_counts[class_name] = class_counts.get(class_name, 0) + 1
    return confidences, class_counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="Path to .pt weights (relative to repo root)")
    parser.add_argument("--imgsz", type=int, default=int(os.environ.get("YOLO_IMGSZ", "960")))
    parser.add_argument("--iterations", type=int, default=30, help="Timed inference runs")
    parser.add_argument("--warmup", type=int, default=5, help="Untimed warmup runs (JIT/cache warmup)")
    parser.add_argument("--camera-id", type=int, default=1, help="Camera to grab the sample frame from")
    parser.add_argument("--synthetic", action="store_true", help="Use a random noise frame instead of a live camera")
    parser.add_argument("--image", type=str, default=None, help="Use a saved image file instead of a live camera")
    parser.add_argument("--cpus", type=int, default=None, help="Pin process to first N CPUs (Linux only, e.g. 2 to match the Oracle VM)")
    parser.add_argument("--csv", type=str, default=_CSV_PATH, help="Output CSV path")
    args = parser.parse_args()

    cpus_pinned = 0
    if args.cpus:
        cpus_pinned = args.cpus if _restrict_cpus(args.cpus) else 0

    from ultralytics import YOLO

    from backend.inference.detector import TARGET_CLASSES

    frame, frame_source = _load_frame(args)

    print(f"Loading model: {args.model}")
    model = YOLO(args.model)

    print(f"Warming up ({args.warmup} runs)...")
    for _ in range(args.warmup):
        model(frame, imgsz=args.imgsz, verbose=False)

    print(f"Timing {args.iterations} inference runs at imgsz={args.imgsz}...")
    durations_ms = []
    last_results = None
    for _ in range(args.iterations):
        t0 = time.perf_counter()
        last_results = model(frame, imgsz=args.imgsz, verbose=False)
        durations_ms.append((time.perf_counter() - t0) * 1000)

    avg_ms = statistics.mean(durations_ms)
    std_ms = statistics.pstdev(durations_ms) if len(durations_ms) > 1 else 0.0
    min_ms = min(durations_ms)
    max_ms = max(durations_ms)
    fps = 1000.0 / avg_ms if avg_ms > 0 else 0.0

    confidences, class_counts = _percent_confidence_stats(last_results, TARGET_CLASSES)
    avg_confidence = statistics.mean(confidences) if confidences else 0.0
    avg_detections = len(confidences)

    print()
    print(f"model            : {args.model}")
    print(f"imgsz            : {args.imgsz}")
    print(f"cpus_pinned      : {cpus_pinned or 'no (all cores)'}")
    print(f"frame_source     : {frame_source}")
    print(f"avg inference    : {avg_ms:.1f} ms  (std {std_ms:.1f}, min {min_ms:.1f}, max {max_ms:.1f})")
    print(f"fps              : {fps:.2f}")
    print(f"detections       : {avg_detections} (avg confidence {avg_confidence:.2f})")
    print(f"per-class counts : {class_counts}")
    print(f"budget check     : {'OK (<1s/frame)' if avg_ms < 1000 else 'OVER BUDGET (>=1s/frame)'}")

    os.makedirs(_LOG_DIR, exist_ok=True)
    write_header = not os.path.exists(args.csv)
    with open(args.csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(_CSV_COLUMNS)
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            args.model, args.imgsz, cpus_pinned or "", platform.platform(),
            frame_source, args.iterations,
            f"{avg_ms:.2f}", f"{std_ms:.2f}", f"{min_ms:.2f}", f"{max_ms:.2f}", f"{fps:.2f}",
            avg_detections, f"{avg_confidence:.3f}", json.dumps(class_counts),
        ])
    print(f"\nAppended to {args.csv}")


if __name__ == "__main__":
    main()
