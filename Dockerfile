FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080

COPY requirements-dashboard.txt .
RUN pip install --no-cache-dir -r requirements-dashboard.txt
COPY dashboard_server.py .
COPY schedule_policy.py .
COPY dashboard ./dashboard

EXPOSE 8080
CMD ["python", "dashboard_server.py"]

