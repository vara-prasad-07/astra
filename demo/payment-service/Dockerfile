FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0

COPY payment_validator.py datadog_logs.py app.py ./

EXPOSE 8100
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8100') + '/health')"

CMD ["python", "app.py"]
