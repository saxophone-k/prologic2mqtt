FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bridge
COPY parser.py controller.py prologic2mqtt.py ./

# Non-root user
RUN useradd -r -u 1001 prologic2mqtt
USER prologic2mqtt

CMD ["python", "-u", "prologic2mqtt.py"]
