FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --user-group --create-home agent
COPY server.py agent.py openapi_tools.py entra_auth.py ./
COPY openapi/ ./openapi/
USER 10001:10001
EXPOSE 8080
CMD ["python", "server.py"]
