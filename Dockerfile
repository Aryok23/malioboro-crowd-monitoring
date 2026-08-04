# Backend image for the Malioboro Monitor demo — built and run on an Oracle
# Cloud Always Free Ampere A1 VM (arm64/aarch64, 2 OCPU, 12GB RAM, CPU-only).
#
# Build for the target VM architecture from an x86 dev machine with buildx:
#   docker buildx build --platform linux/arm64 -t malioboro-backend:latest --load .
#
# python:3.12-slim has official arm64 manifests, and ultralytics/torch/
# opencv-python-headless all ship arm64 (aarch64) wheels on PyPI, so a plain
# `pip install` inside an arm64 container resolves the right wheels — no
# special index or emulation workarounds needed.
#
# NOTE on local testing via QEMU (`docker run --platform linux/arm64` on an
# x86 machine): the build itself works fine under emulation, but actually
# *running* the container this way can segfault inside PyTorch/oneDNN with
# "Can't open MIDR_EL1 sysfs entry" — oneDNN reads that ARM CPU-ID register
# to JIT vectorized kernels, QEMU user-mode emulation doesn't expose it
# correctly, and the generated code crashes. This is an emulation artifact,
# not a bug in the image — cv2/ultralytics import fine under emulation, only
# actual model inference is affected. Verify for real on the target VM
# (native arm64, no emulation) rather than chasing this locally.
FROM python:3.12-slim

WORKDIR /app

# Minimal system libs opencv-python-headless still needs at import time even
# without GUI support (libGL is NOT required for the headless wheel).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first so this layer is cached across code-only changes.
#
# CPU-only torch FIRST, from PyTorch's own CPU index: plain `pip install
# ultralytics` pulls default PyPI torch, which on Linux (amd64 or aarch64)
# drags in the full CUDA stack as separate wheels (nvidia-cublas, nvidia-
# cudnn, triton, cuda-toolkit, ...) — gigabytes of GPU libraries that are
# dead weight on this GPU-less Ampere A1 VM. Installing the CPU wheel first
# satisfies ultralytics' `torch>=1.8.0` dependency so it never reaches for
# the CUDA build.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r backend/requirements.txt \
    # ultralytics unconditionally depends on the GUI `opencv-python` package
    # by name (it won't accept `opencv-python-headless` as a substitute even
    # though both provide the `cv2` module), so the line above installs both
    # — and since opencv-python is resolved after our headless pin, its
    # files clobber headless's, requiring libxcb/libGL etc. that this slim
    # image doesn't have. Force headless back in last so its files win.
    && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

# App code.
COPY backend/ ./backend/

# The chosen model weight — picked after benchmarking n/s/m/l variants of
# YOLOv8 and YOLO11 on a live camera frame (see backend/logs/model_benchmark.csv).
# yolov8nbest.pt: fastest (~14ms/frame on a dev x86 machine), comfortably
# within the <1s/frame budget even accounting for the ARM VM's slower cores,
# and the only camera doing inference at a time now that background polling
# is gone (see backend/stream/camera_manager.py).
COPY yolov8nbest.pt ./yolov8nbest.pt

ENV YOLO_MODEL=yolov8nbest.pt
ENV YOLO_IMGSZ=960
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
