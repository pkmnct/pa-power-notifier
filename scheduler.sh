#!/bin/bash
# Scheduler script to run the rate checker periodically

# Default to daily at 9 AM, but can be overridden
SCHEDULE="${SCHEDULE:-0 9 * * *}"

# Create crontab entry
echo "${SCHEDULE} cd /app && python pa_power_notifier.py >> /data/cron.log 2>&1" | crontab -

# Start cron in foreground
echo "Starting cron scheduler with schedule: ${SCHEDULE}"
echo "Logs will be written to /data/cron.log"
cron -f

