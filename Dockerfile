FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY pa_power_notifier.py .

# Create data directory for persistent storage
RUN mkdir -p /data

# Make script executable
RUN chmod +x pa_power_notifier.py

# Default command (can be overridden)
CMD ["python", "pa_power_notifier.py"]

