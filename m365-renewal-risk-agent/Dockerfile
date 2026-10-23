FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --user-group --create-home bridge
COPY app.py config.py engine.py user_auth.py gcp_auth.py ./
COPY scripts/probe_aca.py ./scripts/probe_aca.py
USER 10001:10001
EXPOSE 8000
CMD ["python", "app.py"]
