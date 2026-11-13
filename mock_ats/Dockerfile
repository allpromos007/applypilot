# Mock ATS container for Cloud Run (deployed in M7).
# Build context is the repo root so the `mock_ats` package path is preserved:
#   docker build -f mock_ats/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MOCK_ATS_DATA_DIR=/tmp/mock_ats_data
WORKDIR /srv

COPY mock_ats/requirements.txt mock_ats/requirements.txt
RUN pip install --no-cache-dir -r mock_ats/requirements.txt

COPY mock_ats/ mock_ats/

# Cloud Run provides $PORT (default 8080).
CMD ["sh", "-c", "uvicorn mock_ats.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
