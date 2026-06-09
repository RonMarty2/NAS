FROM python:3.11-slim

# Zona horaria y locale para nombres en español
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Mexico_City

WORKDIR /app

# Dependencias del sistema mínimas.
# ffmpeg trae ffprobe: permite leer resolución/códec/idioma/duración reales del
# archivo (no solo del nombre) y validar que un duplicado sea legible antes de borrar.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Carpeta de datos persistente (base de datos SQLite)
VOLUME ["/data"]
ENV NAS_DB_PATH=/data/nas.db

EXPOSE 8678

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8678"]
