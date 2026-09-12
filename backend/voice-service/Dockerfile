FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    ffmpeg \
    libgomp1 \
    libsndfile1 \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY voice-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY voice-service /app
EXPOSE 8050
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8050"]
