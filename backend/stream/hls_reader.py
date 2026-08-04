import logging
import os
import queue
import threading
import time
from urllib.parse import urljoin

import cv2
import requests

os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "user_agent;Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
_INITIAL_BACKOFF = 5
_MAX_BACKOFF = 60


def _capture_worker(
    cap: cv2.VideoCapture,
    frame_q: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """
    Daemon thread: calls cap.read() in a tight loop and pushes frames into
    frame_q.  Uses stop_event so the consumer can signal early exit without
    waiting for the 30-second OpenCV/FFmpeg timeout.
    """
    try:
        while not stop_event.is_set():
            ret, frame = cap.read()
            item = frame if (ret and frame is not None) else None
            # Non-blocking put with back-pressure: if the queue is full the
            # consumer is slow/stopped — retry until it drains or we're told
            # to stop.
            while not stop_event.is_set():
                try:
                    frame_q.put(item, timeout=0.5)
                    break
                except queue.Full:
                    continue
    finally:
        cap.release()


class HLSStreamReader:
    """
    Reads frames from an HLS stream.

    Always resolves the live chunklist from master.m3u8 so it survives
    Wowza token rotations.  Reconnects with exponential back-off on drop.

    cap.read() is moved into a daemon thread so stop() returns immediately
    instead of waiting up to 30 s for the OpenCV/FFmpeg timeout.
    """

    def __init__(self, master_url: str, fps_limit: float = 1.0):
        self.master_url = master_url
        self.fps_limit = max(fps_limit, 0.01)
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stream_frames(self):
        """Generator that yields numpy BGR frames until stop() is called."""
        backoff = _INITIAL_BACKOFF

        while not self._stop_event.is_set():
            chunklist_url = self._resolve_chunklist()
            if not chunklist_url:
                logger.warning(
                    "Cannot resolve chunklist from %s — retrying in %ds",
                    self.master_url,
                    backoff,
                )
                self._sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue

            backoff = _INITIAL_BACKOFF

            cap = cv2.VideoCapture(chunklist_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                logger.warning(
                    "Cannot open capture for %s — retrying in %ds",
                    chunklist_url,
                    backoff,
                )
                cap.release()
                self._sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue

            logger.info("Stream opened: %s (fps_limit=%.1f)", self.master_url, self.fps_limit)

            # Per-connection stop event so we can tear down this specific
            # capture worker without affecting the outer reconnect loop.
            conn_stop = threading.Event()
            frame_q: queue.Queue = queue.Queue(maxsize=2)
            worker = threading.Thread(
                target=_capture_worker,
                args=(cap, frame_q, conn_stop),
                daemon=True,
            )
            worker.start()

            frame_interval = 1.0 / self.fps_limit
            last_yield = 0.0
            consecutive_failures = 0
            reconnect = False

            while not self._stop_event.is_set():
                try:
                    frame = frame_q.get(timeout=1.0)
                except queue.Empty:
                    continue

                if frame is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        logger.warning(
                            "5 consecutive read failures on %s — reconnecting",
                            self.master_url,
                        )
                        reconnect = True
                        break
                    continue

                consecutive_failures = 0

                now = time.monotonic()
                elapsed = now - last_yield
                if elapsed < frame_interval:
                    self._sleep(frame_interval - elapsed)

                if self._stop_event.is_set():
                    break

                last_yield = time.monotonic()
                yield frame

            # Tear down this connection's worker.  It's a daemon thread so it
            # will be killed on process exit even if cap.read() is still blocking.
            conn_stop.set()
            worker.join(timeout=2)

            if not reconnect:
                # stop() was called externally — exit the outer loop.
                break

            if not self._stop_event.is_set():
                logger.info(
                    "Stream dropped for %s — reconnecting in %ds",
                    self.master_url,
                    backoff,
                )
                self._sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def stop(self) -> None:
        """Signal the reader to stop. Returns immediately."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_chunklist(self) -> str | None:
        """
        Fetch master.m3u8 and return the absolute URL of the first
        non-comment, non-empty line (the chunklist).
        """
        try:
            resp = requests.get(self.master_url, headers=_HEADERS, timeout=10)
            resp.raise_for_status()
            for line in resp.text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.startswith("http"):
                        return line
                    return urljoin(self.master_url, line)
        except Exception as exc:
            logger.debug("Failed to fetch master playlist %s: %s", self.master_url, exc)
        return None

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep that respects stop_event."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return
            time.sleep(min(0.5, deadline - time.monotonic()))
