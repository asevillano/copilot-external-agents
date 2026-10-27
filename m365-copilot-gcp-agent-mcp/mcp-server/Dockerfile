FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --user-group --create-home mcp
COPY server.py agent_engine.py gcp_auth.py entra_auth.py ./
USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]
