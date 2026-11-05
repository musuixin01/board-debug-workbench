FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BOARD_DEBUG_DB=/data/board-debug.sqlite3 BOARD_DEBUG_UPLOADS=/data/uploads
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py hardware_debug.py ./
COPY static ./static
RUN mkdir /data
EXPOSE 18763
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18763"]
