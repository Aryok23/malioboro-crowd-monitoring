import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

load_dotenv()

from .inference.detector import get_counts, load_model
from .stream.camera_manager import (
    CameraManager,
    _latest_jpegs,
    _latest_jpegs_lock,
    evaluate_threshold_alerts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load camera list
# ---------------------------------------------------------------------------

_CAMERAS_FILE = os.path.join(os.path.dirname(__file__), "config", "cameras.json")
with open(_CAMERAS_FILE, encoding="utf-8") as _f:
    CAMERAS: list[dict] = json.load(_f)
_CAMERA_MAP: dict[int, dict] = {c["id"]: c for c in CAMERAS}

_HLS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ---------------------------------------------------------------------------
# Config: CORS + simple rate limiting (this is a stateless, unauthenticated
# demo deployment on a 2-OCPU free-tier VM — these limits exist purely to
# stop a single misbehaving client from hammering the box, not for security).
# ---------------------------------------------------------------------------

_CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGIN", "http://localhost:5173").split(",") if o.strip()]
_RATE_LIMIT_SWITCH_PER_MIN = int(os.environ.get("RATE_LIMIT_SWITCH_PER_MIN", "20"))
_MAX_WS_CONNECTIONS = int(os.environ.get("MAX_WS_CONNECTIONS", "5"))
_ALERT_COOLDOWN_SECS = 300

# ---------------------------------------------------------------------------
# Shared state (all in-memory — no DB, no disk persistence)
# ---------------------------------------------------------------------------

_camera_manager: Optional[CameraManager] = None
_ws_queues: list[asyncio.Queue] = []
_loop: Optional[asyncio.AbstractEventLoop] = None
_last_alert: dict[tuple, datetime] = {}
_switch_request_log: dict[str, deque] = {}


def _rate_limited(ip: str) -> bool:
    """Sliding 60s window, per client IP. In-memory only — resets on restart,
    which is fine for a single-process demo deployment."""
    now = time.monotonic()
    log = _switch_request_log.setdefault(ip, deque())
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= _RATE_LIMIT_SWITCH_PER_MIN:
        return True
    log.append(now)
    return False


def _trigger_value(alert_type: str, counts: dict) -> int:
    """HIGH_CROWD -> pedestrian count; HIGH_TRAFFIC -> all non-pedestrian
    classes summed. `counts` keys are the Indonesian class names from
    detector.get_counts() — "orang" is people."""
    if alert_type == "HIGH_CROWD":
        return counts.get("orang", 0)
    return sum(v for k, v in counts.items() if k != "orang")


def _check_alerts(camera: dict, counts: dict) -> list[dict]:
    """In-memory threshold + 5-minute cooldown check per (camera, alert_type).
    No DB, no image capture — the caller broadcasts the result straight over
    the /ws/active payload."""
    now = datetime.utcnow()
    fired = []
    for alert_type, description in evaluate_threshold_alerts(camera, counts):
        key = (camera["id"], alert_type)
        last = _last_alert.get(key)
        if last and (now - last).total_seconds() < _ALERT_COOLDOWN_SECS:
            continue
        _last_alert[key] = now
        fired.append({
            "alert_type": alert_type,
            "description": description,
            "trigger_value": _trigger_value(alert_type, counts),
            "timestamp": now.isoformat(),
        })
    return fired


# ---------------------------------------------------------------------------
# Callback (called from the active camera's background inference thread)
# ---------------------------------------------------------------------------


def _on_active_frame(camera_id: int, frame, detections: list) -> None:
    counts = get_counts(detections)
    h, w = frame.shape[:2] if frame is not None else (720, 1280)
    camera = _CAMERA_MAP.get(camera_id, {})

    payload = {
        "type": "detection",
        "camera_id": camera_id,
        "detections": detections,
        "counts": counts,
        "frame_width": w,
        "frame_height": h,
        "timestamp": datetime.utcnow().isoformat(),
        "alerts": _check_alerts(camera, counts),
    }

    for q in list(_ws_queues):
        try:
            _loop.call_soon_threadsafe(q.put_nowait, payload)
        except (asyncio.QueueFull, Exception):
            pass


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _camera_manager, _loop

    _loop = asyncio.get_event_loop()

    yolo_model = os.getenv("YOLO_MODEL", "yolov8nbest.pt")
    logger.info("Loading YOLO_MODEL=%s YOLO_IMGSZ=%s", yolo_model, os.getenv("YOLO_IMGSZ", "960"))
    load_model(yolo_model)

    _camera_manager = CameraManager(CAMERAS, _on_active_frame)
    _camera_manager.start(default_camera_id=1)

    yield

    _camera_manager.stop()

    # Skip Python's OpenCV/FFmpeg finalizers — they block for 30 s waiting for
    # cap.read() daemon threads to time out.  All real cleanup (camera stop)
    # is already done above, so a hard exit is safe here.
    logging.shutdown()
    os._exit(0)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Malioboro Monitor API (demo)", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# HLS Proxy
# ---------------------------------------------------------------------------

def _rewrite_m3u8(content: str, camera_id: int) -> str:
    """Rewrite relative URLs inside an m3u8 playlist to go through our proxy."""
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if stripped.startswith("http"):
                # Absolute URL: keep only the filename, proxy it
                filename = stripped.rsplit("/", 1)[-1]
                lines.append(f"/proxy/hls/{camera_id}/{filename}")
            else:
                lines.append(f"/proxy/hls/{camera_id}/{stripped}")
        else:
            lines.append(line)
    return "\n".join(lines)


@app.get("/proxy/hls/{camera_id}/master.m3u8")
async def proxy_master(camera_id: int):
    camera = _CAMERA_MAP.get(camera_id)
    if not camera:
        raise HTTPException(status_code=404)

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(camera["master_url"], headers=_HLS_HEADERS, timeout=15, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Upstream error: {exc}")

    rewritten = _rewrite_m3u8(resp.text, camera_id)
    return Response(
        content=rewritten,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/proxy/hls/{camera_id}/{filename:path}")
async def proxy_hls_segment(camera_id: int, filename: str):
    camera = _CAMERA_MAP.get(camera_id)
    if not camera:
        raise HTTPException(status_code=404)

    base_url = camera["master_url"].rsplit("/", 1)[0] + "/"
    target_url = base_url + filename

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(target_url, headers=_HLS_HEADERS, timeout=20, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Upstream error: {exc}")

    content_type = resp.headers.get("content-type", "application/octet-stream")

    # Rewrite m3u8 chunklists too (chunklist contains TS segment URLs)
    if filename.endswith(".m3u8") or "mpegurl" in content_type:
        rewritten = _rewrite_m3u8(resp.text, camera_id)
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    return Response(content=resp.content, media_type=content_type)


# ---------------------------------------------------------------------------
# MJPEG stream (used as a lightweight preview; img tag fetches directly)
# ---------------------------------------------------------------------------


@app.get("/stream/mjpeg/{camera_id}")
async def stream_mjpeg(camera_id: int):
    if camera_id not in _CAMERA_MAP:
        raise HTTPException(status_code=404, detail=f"Camera {camera_id} not found")

    async def generator():
        while True:
            with _latest_jpegs_lock:
                frame_bytes = _latest_jpegs.get(camera_id)
            if frame_bytes:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + frame_bytes
                    + b"\r\n"
                )
            await asyncio.sleep(0.1)

    return StreamingResponse(
        generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------


@app.get("/cameras")
async def get_cameras():
    return CAMERAS


@app.post("/camera/switch/{camera_id}")
async def switch_camera(camera_id: int, request: Request):
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests — slow down")
    if _camera_manager is None:
        raise HTTPException(status_code=503, detail="Camera manager not ready")
    if camera_id not in _CAMERA_MAP:
        raise HTTPException(status_code=404, detail=f"Camera {camera_id} not found")
    _camera_manager.switch_active_camera(camera_id)
    return {"status": "switched", "camera_id": camera_id}


# ---------------------------------------------------------------------------
# WebSocket — detection results + alerts, no frame data, no auth
# ---------------------------------------------------------------------------


@app.websocket("/ws/active")
async def ws_active(websocket: WebSocket):
    if len(_ws_queues) >= _MAX_WS_CONNECTIONS:
        await websocket.close(code=1013)  # 1013 = Try Again Later
        return

    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    _ws_queues.append(q)
    # One persistent receive task — completes when uvicorn sends a disconnect
    # during shutdown (connection.shutdown() puts a disconnect msg in the ASGI
    # receive queue).  Without this, the handler never sees the signal and
    # uvicorn blocks forever at "Waiting for connections to close."
    recv_task = asyncio.create_task(websocket.receive())
    get_task: asyncio.Task | None = None
    try:
        while True:
            get_task = asyncio.create_task(q.get())
            done, _ = await asyncio.wait(
                {get_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if recv_task in done:
                get_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await get_task
                break
            payload = get_task.result()
            if payload is None:  # shutdown sentinel
                break
            try:
                await websocket.send_text(json.dumps(payload))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket error: %s", exc)
    finally:
        if get_task is not None and not get_task.done():
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await get_task
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await recv_task
        if q in _ws_queues:
            _ws_queues.remove(q)
