# Laya (github.com/NandhaKishorM/laya) reads each Auto message and names its task type for ModelMesh's router.
# CPU-only PyTorch keeps the image small; the reader's weights download on first use into the /models volume.
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 HF_HOME=/models LAYA_DEVICE=cpu PYTHONUNBUFFERED=1
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install "laya[serve]==0.4.26"
WORKDIR /app
COPY server.py train.py labels.json training.jsonl ./
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
