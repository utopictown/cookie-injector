FROM python:3.11-slim

WORKDIR /app

# Install only runtime deps (no browser install needed — we connect remotely)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# playwright package gives us the async API
# We connect via CDP to browserless — no local browser needed

COPY app.py .

# Data volume for saved cookies between sessions
VOLUME [ "/data" ]
EXPOSE 9224 8001

CMD ["python", "-u", "app.py"]
