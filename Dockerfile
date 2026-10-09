FROM node:24-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libgles2 libegl1 libglib2.0-0 libportaudio2 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY scripts/ scripts/
COPY --from=frontend /build/dist frontend/dist/
RUN useradd --uid 10001 --create-home dtr && mkdir /app/data && chown dtr:dtr /app/data
USER dtr
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
