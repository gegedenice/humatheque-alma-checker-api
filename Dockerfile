FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd -m appuser

COPY requirements.txt /app/requirements.txt
RUN pip install -U pip && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 8000

USER appuser

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-keep-alive 0"]
