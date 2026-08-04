import csv
import logging
import os
import statistics
import threading
import time
from datetime import datetime

import numpy as np

logger = logging.getLogger(__name__)

TARGET_CLASSES = {
    "andong", "bajaj", "becak", "sepeda",
    "bus", "mobil", "motor", "orang", "truk",
}

_model = None
_model_path: str | None = None
_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "960"))

# --- inference speed logging (pure measurement, does not affect detection output) ---
_SPEED_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_SPEED_LOG_PATH = os.path.join(_SPEED_LOG_DIR, "inference_speed.csv")
_SPEED_LOG_SUMMARY_INTERVAL = int(os.environ.get("INFERENCE_LOG_SUMMARY_INTERVAL", "100"))
_speed_log_lock = threading.Lock()
_speed_log_header_written = False
_speed_log_count = 0
_speed_log_window: list[float] = []


def load_model(model_path: str = "yolov8nbest.pt") -> None:
    global _model, _model_path
    from ultralytics import YOLO
    logger.info("Loading YOLO model: %s", model_path)
    _model = YOLO(model_path)
    _model_path = model_path
    logger.info("YOLO model loaded.")


def detect(frame: np.ndarray, camera_id: int | None = None) -> list[dict]:
    if _model is None:
        return []
    try:
        t0 = time.perf_counter()
        results = _model(frame, imgsz=_IMGSZ, verbose=False)
        inference_ms = (time.perf_counter() - t0) * 1000
        _log_inference_speed(camera_id, inference_ms)
        return _parse_results(results)
    except Exception as exc:
        logger.warning("Inference error: %s", exc)
        return []


def _log_inference_speed(camera_id: int | None, inference_ms: float) -> None:
    global _speed_log_header_written, _speed_log_count
    with _speed_log_lock:
        os.makedirs(_SPEED_LOG_DIR, exist_ok=True)
        if not _speed_log_header_written:
            _speed_log_header_written = os.path.exists(_SPEED_LOG_PATH)
        write_header = not _speed_log_header_written
        with open(_SPEED_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["timestamp", "camera_id", "inference_ms", "imgsz", "model_path"])
            writer.writerow([
                datetime.now().isoformat(timespec="milliseconds"),
                camera_id if camera_id is not None else "",
                f"{inference_ms:.3f}",
                _IMGSZ,
                _model_path,
            ])
        _speed_log_header_written = True

        _speed_log_window.append(inference_ms)
        if len(_speed_log_window) > _SPEED_LOG_SUMMARY_INTERVAL:
            del _speed_log_window[:-_SPEED_LOG_SUMMARY_INTERVAL]

        _speed_log_count += 1
        if _speed_log_count % _SPEED_LOG_SUMMARY_INTERVAL == 0:
            avg = statistics.mean(_speed_log_window)
            std = statistics.pstdev(_speed_log_window) if len(_speed_log_window) > 1 else 0.0
            fps = 1000.0 / avg if avg > 0 else 0.0
            logger.info(
                "[inference-speed] last %d inferences: avg=%.2fms std=%.2fms fps=%.2f "
                "(model=%s, imgsz=%s)",
                len(_speed_log_window), avg, std, fps, _model_path, _IMGSZ,
            )


def get_counts(detections: list[dict]) -> dict:
    counts = {cls: 0 for cls in TARGET_CLASSES}
    for det in detections:
        cls = det.get("class_name")
        if cls in counts:
            counts[cls] += 1
    return counts


def _parse_results(results) -> list[dict]:
    detections = []
    for result in results:
        if result.boxes is None:
            continue
        for box in result.boxes:
            class_name = result.names.get(int(box.cls[0]), "")
            if class_name not in TARGET_CLASSES:
                continue
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
            detections.append({
                "class_name": class_name,
                "confidence": round(float(box.conf[0]), 2),
                "bbox": [x1, y1, x2, y2],
            })
    return detections
