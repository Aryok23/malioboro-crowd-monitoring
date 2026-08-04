# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Simplified, portfolio-focused deployment of an undergraduate thesis (skripsi) project: real-time visitor and traffic density monitoring for the Malioboro area, Yogyakarta, Indonesia. Ingests live HLS streams from 22 public CCTV cameras, runs YOLO (Ultralytics) object detection on whichever camera the user is currently viewing, and displays results in a React dashboard.

This version is deliberately **stateless and unauthenticated** — no database, no login, no background polling of idle cameras — so it can run on a free-tier 2-OCPU VM and be shared publicly. See README.md's "What's different from the original thesis system" for the full list of simplifications.

## Commands

### Backend
```bash
# First time setup (run from repo root)
python -m venv venv
venv\Scripts\activate
pip install -r backend/requirements.txt

# Run (--timeout-graceful-shutdown required for clean Ctrl+C exit)
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5
```

No database to reset, no admin user to seed — nothing to do besides `.env` (copy from `.env.example`).

### Frontend
```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
npm run build
```

### Docker
```bash
docker compose up -d --build                 # local, native arch
docker buildx build --platform linux/arm64 -t malioboro-backend:latest --load .   # target VM arch
```

### Benchmarking a model before switching `YOLO_MODEL`
```bash
python -m backend.scripts.benchmark_model --model yolov8nbest.pt
python -m backend.scripts.benchmark_model --model yolov8nbest.pt --cpus 2   # Linux only, matches the 2-OCPU VM
```

## Architecture

### Video pipeline
`LiveFeed.jsx` displays the active camera as an MJPEG stream (`<img src="/stream/mjpeg/{camera_id}">`, multipart JPEG pushed from the backend's in-memory `_latest_jpegs` cache) — **not** `hls.js`; that package isn't a dependency here. The backend also exposes an HLS proxy at `/proxy/hls/{camera_id}/master.m3u8` (rewrites playlist URLs to avoid CCTV server CORS issues), used if you wire up a direct HLS player instead. The WebSocket `/ws/active` carries only detection results + alerts (counts, bboxes, frame dims, any triggered alerts) — no frame data.

### Detection pipeline
`CameraManager` (`backend/stream/camera_manager.py`) runs a single `_ActiveCameraProcessor` for whichever camera the user currently has open — one thread drains the HLS stream continuously (`fps_limit=30`), a second thread runs YOLO inference on the latest buffered frame every 0.5s (~2 Hz) and pushes results to WebSocket queues via `loop.call_soon_threadsafe`. Switching cameras (`POST /camera/switch/{id}`) stops the old processor and starts a new one. There is **no background polling** of the other 21 cameras — that was removed to keep load predictable on a 2-core VM; each camera only gets inference while a client is actively viewing it.

### Alerts — in-memory only
`evaluate_threshold_alerts()` checks per-camera thresholds from `cameras.json`'s `alert_thresholds` (falling back to global `ALERT_PEOPLE_THRESHOLD`/`ALERT_VEHICLE_THRESHOLD` env vars if a camera defines none). `backend/main.py`'s `_check_alerts()` applies a 5-minute cooldown per (camera_id, alert_type) using an in-memory dict (`_last_alert`) and embeds any triggered alerts directly in the next `/ws/active` payload's `"alerts"` field. Nothing is written to disk or a database — the frontend (`Dashboard.jsx`) accumulates alerts client-side into a capped in-memory list, and the 15-minute rolling trend chart (`CrowdChart.jsx`) is built the same way from raw WS samples. Both reset on page reload or backend restart.

### Frontend bbox overlay
`LiveFeed.jsx` stacks a `<canvas>` over the MJPEG `<img>`. On each WebSocket detection message, `drawDetections()` scales bbox coordinates from the original OpenCV frame dimensions (`frame_width`/`frame_height` in the WS payload) to the canvas display size, accounting for `object-fit: contain` letterboxing.

### No database, no auth
Both were removed for this demo (see `backend/database/`, `backend/auth/` — they don't exist anymore). CORS is restricted via the `CORS_ORIGIN` env var (comma-separated origins). Simple in-memory per-IP rate limiting protects `/camera/switch/{id}` (`RATE_LIMIT_SWITCH_PER_MIN`) and `/ws/active` caps concurrent connections (`MAX_WS_CONNECTIONS`) — see `backend/main.py`. These are anti-abuse guards for a free-tier VM, not real access control.

### Detection classes
9 target classes, using the **Indonesian** names the model itself outputs (`backend/inference/detector.py` `TARGET_CLASSES`) — there is no COCO-name mapping step: `orang` (people), `sepeda` (bicycle), `motor` (motorcycle), `bajaj`, `becak`, `andong`, `mobil` (car), `bus`, `truk` (truck). Default model is `yolov8nbest.pt` — chosen after benchmarking n/s/m/l variants of YOLOv8 and YOLO11 (see `backend/scripts/benchmark_model.py` and `backend/logs/model_benchmark.csv`) for the fastest inference, since the target Oracle VM has only 2 shared CPU cores and no GPU. Override via `YOLO_MODEL=<path>.pt` and `YOLO_IMGSZ=<int>` in `.env`; any model must output class names matching `TARGET_CLASSES` or its detections are silently filtered out.

### Camera list
22 cameras in `backend/config/cameras.json`. Cameras 1–18 use `/malioboro/` base path; cameras 19–21 (Nol KM) use `/malioboro/` but camera 22 (`ATCS_kmnol`) uses `/atcs/`. If a Nol KM stream fails, verify the base path.

## Key files

| File | Role |
|------|------|
| `backend/main.py` | FastAPI app, HLS proxy, WebSocket broadcast, in-memory alert logic, rate limiting |
| `backend/stream/hls_reader.py` | Reads HLS stream, resolves master→chunklist, exponential backoff reconnect |
| `backend/stream/camera_manager.py` | Active-camera processor only (no background pool) + alert threshold evaluation |
| `backend/inference/detector.py` | YOLO (Ultralytics) wrapper; filters model output against `TARGET_CLASSES` (Indonesian names) |
| `backend/scripts/benchmark_model.py` | Standalone script to time candidate `.pt` weights before picking one |
| `frontend/src/pages/Dashboard.jsx` | WebSocket lifecycle, state owner for detections/frameSize/cameras/alerts/trend |
| `frontend/src/components/LiveFeed.jsx` | MJPEG `<img>` player + canvas bbox overlay |
| `frontend/src/components/CrowdChart.jsx` | Client-side 15-minute rolling trend chart (no backend history) |
| `frontend/src/components/AlertsPanel.jsx` | Session-only alert log, purely presentational (data comes from Dashboard's WS state) |
| `backend/config/cameras.json` | Camera registry (id, name, zone, master_url, is_ptz, alert_thresholds) |

## Important notes

- **`__init__.py` files are required** in all backend subpackages — relative imports depend on them
- The `_on_active_frame` callback is called from a background thread — always use `loop.call_soon_threadsafe` or `asyncio.run_coroutine_threadsafe` to interact with the event loop from it
- Alert cooldown is 5 minutes per (camera_id, alert_type) pair, in-memory (`_last_alert` dict in `main.py`) — resets on restart
- Only one `.pt` file is kept in the repo root (`yolov8nbest.pt`, the chosen model) — the other 8 benchmarked variants were deleted after the model was picked; re-download/retrain if you need to re-benchmark alternatives
