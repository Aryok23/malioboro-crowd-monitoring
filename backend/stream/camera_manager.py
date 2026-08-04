import logging
import os
import threading
import time
from typing import Callable

from ..inference.detector import detect
from .hls_reader import HLSStreamReader

logger = logging.getLogger(__name__)

_latest_jpegs: dict[int, bytes] = {}
_latest_jpegs_lock = threading.Lock()

# Global fallback thresholds (ALERT_PEOPLE_THRESHOLD / ALERT_VEHICLE_THRESHOLD
# env vars) used only for cameras that don't define their own
# "alert_thresholds" in cameras.json — a per-camera entry always wins.
_GLOBAL_PEOPLE_THRESHOLD = os.environ.get("ALERT_PEOPLE_THRESHOLD")
_GLOBAL_PEOPLE_THRESHOLD = int(_GLOBAL_PEOPLE_THRESHOLD) if _GLOBAL_PEOPLE_THRESHOLD else None

_GLOBAL_VEHICLE_THRESHOLD = os.environ.get("ALERT_VEHICLE_THRESHOLD")
_GLOBAL_VEHICLE_THRESHOLD = int(_GLOBAL_VEHICLE_THRESHOLD) if _GLOBAL_VEHICLE_THRESHOLD else None


def evaluate_threshold_alerts(camera: dict, counts: dict) -> list[tuple[str, str]]:
    """
    Per-camera, per-class alert thresholds from cameras.json's "alert_thresholds".
    A camera that defines its own threshold for a class always uses that value;
    a camera with no thresholds at all falls back to the global
    ALERT_PEOPLE_THRESHOLD / ALERT_VEHICLE_THRESHOLD env vars (the latter
    applied to the summed non-pedestrian count, since there's no per-class
    global default). Returns the (alert_type, description) pairs triggered
    this call; alert_type is always "HIGH_CROWD" (class "orang") or
    "HIGH_TRAFFIC" (any other class).
    """
    thresholds = camera.get("alert_thresholds") or {}
    triggered: list[tuple[str, str]] = []

    crowd_limit = thresholds.get("orang", _GLOBAL_PEOPLE_THRESHOLD)
    people = counts.get("orang", 0)
    if crowd_limit is not None and people > crowd_limit:
        triggered.append(("HIGH_CROWD", f"orang={people} melebihi ambang {crowd_limit}"))

    vehicle_thresholds = {k: v for k, v in thresholds.items() if k != "orang"}
    if vehicle_thresholds:
        exceeded = [
            f"{cls}={counts.get(cls, 0)}>{limit}"
            for cls, limit in vehicle_thresholds.items()
            if counts.get(cls, 0) > limit
        ]
        if exceeded:
            triggered.append(("HIGH_TRAFFIC", "Ambang kendaraan terlampaui: " + ", ".join(exceeded)))
    elif _GLOBAL_VEHICLE_THRESHOLD is not None:
        total_vehicles = sum(v for k, v in counts.items() if k != "orang")
        if total_vehicles > _GLOBAL_VEHICLE_THRESHOLD:
            triggered.append((
                "HIGH_TRAFFIC",
                f"total kendaraan={total_vehicles} melebihi ambang {_GLOBAL_VEHICLE_THRESHOLD}",
            ))

    return triggered


class _ActiveCameraProcessor:
    """Two threads: one drains the HLS stream, one runs inference at a fixed interval."""

    def __init__(self, camera: dict, on_active_frame: Callable, detection_interval: float = 0.5):
        self._camera = camera
        self._on_active_frame = on_active_frame
        self._detection_interval = detection_interval
        self._reader = HLSStreamReader(camera["master_url"], fps_limit=30.0)
        self._reader_thread: threading.Thread | None = None
        self._inference_thread: threading.Thread | None = None
        self._running = False
        self._latest_frame = None
        self._frame_lock = threading.Lock()

    def start(self) -> None:
        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"active-reader-{self._camera['id']}"
        )
        self._inference_thread = threading.Thread(
            target=self._inference_loop, daemon=True, name=f"active-infer-{self._camera['id']}"
        )
        self._reader_thread.start()
        self._inference_thread.start()

    def stop(self) -> None:
        self._running = False
        self._reader.stop()
        if self._reader_thread:
            self._reader_thread.join(timeout=5)
        if self._inference_thread:
            self._inference_thread.join(timeout=5)

    @property
    def camera_id(self) -> int:
        return self._camera["id"]

    def _reader_loop(self) -> None:
        for frame in self._reader.stream_frames():
            if not self._running:
                break
            with self._frame_lock:
                self._latest_frame = frame

    def _inference_loop(self) -> None:
        import os
        import cv2
        if os.environ.get("DEBUG_SAVE_FRAMES") == "1":
            os.makedirs(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "debug_frames",
                ),
                exist_ok=True,
            )
        next_tick = time.monotonic()
        while self._running:
            next_tick += self._detection_interval

            with self._frame_lock:
                frame = self._latest_frame

            if frame is not None:
                try:
                    t0 = time.monotonic()
                    detections = detect(frame, camera_id=self._camera['id'])
                    logger.debug("cam %d inference=%.3fs", self._camera['id'], time.monotonic() - t0)
                    ok, buf = cv2.imencode('.jpg', frame)
                    if ok:
                        with _latest_jpegs_lock:
                            _latest_jpegs[self._camera['id']] = buf.tobytes()
                    self._on_active_frame(self._camera["id"], frame, detections)
                    if os.environ.get("DEBUG_SAVE_FRAMES") == "1":
                        debug_dir = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "debug_frames",
                        )
                        ts_ms = int(time.time() * 1000)
                        fname = f"{self._camera['id']}_{ts_ms}.jpg"
                        debug_frame = frame.copy()
                        for det in detections:
                            x1, y1, x2, y2 = det["bbox"]
                            label = f"{det['class_name']} {det['confidence']}"
                            cv2.rectangle(debug_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                            cv2.putText(
                                debug_frame, label, (x1, max(y1 - 5, 0)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1,
                            )
                        cv2.imwrite(os.path.join(debug_dir, fname), debug_frame)
                except Exception as exc:
                    logger.error("Error in active frame callback: %s", exc)

            sleep_time = next_tick - time.monotonic()
            logger.debug("cam %d sleep=%.3fs", self._camera['id'], sleep_time)
            if sleep_time > 0:
                time.sleep(sleep_time)


class CameraManager:
    """
    Public façade that owns the active-camera processor. Only the camera the
    user currently has open runs inference — there is no background polling
    of the other cameras (removed to keep the 2-OCPU deployment VM's load
    predictable: one stream, one inference loop, at all times).

    Callbacks are called from background threads — callers must be thread-safe.
    """

    def __init__(self, cameras: list[dict], on_active_frame: Callable):
        self._cameras = cameras
        self._on_active_frame = on_active_frame
        self._active_processor: _ActiveCameraProcessor | None = None

    def start(self, default_camera_id: int = 1) -> None:
        camera = self._camera_by_id(default_camera_id)
        if camera is None:
            raise ValueError(f"Camera id={default_camera_id} not found in cameras list")

        self._active_processor = _ActiveCameraProcessor(camera, self._on_active_frame)
        self._active_processor.start()
        logger.info("CameraManager started with active camera id=%d", default_camera_id)

    def switch_active_camera(self, camera_id: int) -> None:
        camera = self._camera_by_id(camera_id)
        if camera is None:
            raise ValueError(f"Camera id={camera_id} not found in cameras list")

        if self._active_processor:
            self._active_processor.stop()

        self._active_processor = _ActiveCameraProcessor(camera, self._on_active_frame)
        self._active_processor.start()
        logger.info("Active camera switched to id=%d", camera_id)

    def stop(self) -> None:
        if self._active_processor:
            self._active_processor.stop()
        logger.info("CameraManager stopped.")

    def _camera_by_id(self, camera_id: int) -> dict | None:
        return next((c for c in self._cameras if c["id"] == camera_id), None)
