# Mock ATS container for Cloud Run.
# Build context is the mock_ats/ folder:
#   gcloud run deploy mock-ats --source mock_ats --region us-central1 --no-allow-unauthenticated
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MOCK_ATS_DATA_DIR=/tmp/mock_ats_data
WORKDIR /srv

COPY requirements.txt mock_ats/requirements.txt
RUN pip install --no-cache-dir -r mock_ats/requirements.txt

# Keep the package path so `mock_ats.main:app` resolves.
COPY . mock_ats/

# Cloud Run provides $PORT (default 8080).
CMD ["sh", "-c", "uvicorn mock_ats.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
