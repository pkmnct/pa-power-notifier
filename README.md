# PA Power & Gas Rate Notifier

A Docker-based utility that periodically checks for potential savings on Pennsylvania electric and gas utilities by comparing current rates with available offers from [PA Power Switch](https://www.papowerswitch.com) and [PA Gas Switch](https://www.pagasswitch.com).

## Features

- 🔌 Checks both electric and gas rates
- 📊 Compares rates and identifies savings opportunities
- ⚙️ Fully configurable via environment variables
- 🐳 Dockerized for easy deployment
- ⏰ Supports periodic scheduling
- 💾 Stores rate history for comparison

## Quick Start

### Option 1: Run Once (Manual Check)

```bash
docker build -t pa-power-notifier .
docker run --rm \
  -e CURRENT_ELECTRIC_PRICE=0.12 \
  -e CURRENT_GAS_PRICE=0.45 \
  -e ELECTRIC_ID=1180 \
  -e GAS_ID=4423 \
  -v $(pwd)/data:/data \
  pa-power-notifier
```

### Option 2: Run Periodically (Scheduled)

```bash
# Build the scheduler image
docker build -f Dockerfile.scheduler -t pa-power-notifier-scheduler .

# Run with daily checks at 9 AM
docker run -d \
  --name pa-power-notifier \
  -e CURRENT_ELECTRIC_PRICE=0.12 \
  -e CURRENT_GAS_PRICE=0.45 \
  -e ELECTRIC_ID=1180 \
  -e GAS_ID=4423 \
  -e SCHEDULE="0 9 * * *" \
  -v $(pwd)/data:/data \
  pa-power-notifier-scheduler
```

### Option 3: Using Docker Compose

1. Create a `.env` file:

```bash
# Electric API configuration
ELECTRIC_ID=1180
ELECTRIC_SERVICE_TYPE=residential
ELECTRIC_RATE_TYPE=RS - Regular Residential Service

# Gas API configuration
GAS_ID=4423
GAS_SERVICE_TYPE=residential

# Your current rates (set these!)
CURRENT_ELECTRIC_PRICE=0.12
CURRENT_GAS_PRICE=0.45

# Notification threshold (percentage savings to notify)
NOTIFICATION_THRESHOLD=5.0
```

2. Run with docker-compose:

```bash
# Run once
docker-compose up

# Run in background
docker-compose up -d

# View logs
docker-compose logs -f
```

## Configuration

All configuration is done via environment variables:

### Electric API Settings

- `ELECTRIC_ID` (default: `1180`) - Your electric utility ID
- `ELECTRIC_SERVICE_TYPE` (default: `residential`) - Service type
- `ELECTRIC_RATE_TYPE` (default: `RS - Regular Residential Service`) - Rate type

### Gas API Settings

- `GAS_ID` (default: `4423`) - Your gas utility ID
- `GAS_SERVICE_TYPE` (default: `residential`) - Service type

### Current Rates

- `CURRENT_ELECTRIC_PRICE` (default: `0`) - Your current electric rate per kWh
- `CURRENT_GAS_PRICE` (default: `0`) - Your current gas rate per therm/ccf

**Important:** Set your current rates to enable savings comparison!

### Notification Settings

- `NOTIFICATION_THRESHOLD` (default: `5.0`) - Minimum percentage savings to notify (e.g., `5.0` means 5%)

### Scheduling (Scheduler Mode Only)

- `SCHEDULE` (default: `0 9 * * *`) - Cron schedule format (runs daily at 9 AM by default)

Examples:
- `0 9 * * *` - Daily at 9 AM
- `0 */6 * * *` - Every 6 hours
- `0 9 * * 1` - Every Monday at 9 AM

## Finding Your Configuration Values

### Finding Your Electric ID and Rate Type

1. Visit [PA Power Switch](https://www.papowerswitch.com)
2. Enter your zip code and select your utility
3. Look at the URL or API calls to find your `id` and `ratetype` parameters

### Finding Your Gas ID

1. Visit [PA Gas Switch](https://www.pagasswitch.com)
2. Enter your zip code and select your utility
3. Look at the URL or API calls to find your `id` parameter

## Output

The script will:

1. Fetch current rates from both APIs
2. Compare with your current rates
3. Display any savings opportunities above the threshold
4. Save rate history to `/data/previous_rates.json`

### Example Output

```
============================================================
🔌 PA Utility Savings Opportunities Found!

⚡ ELECTRIC Savings Available:
  Current Price: $0.1200
  Best Price: $0.0899
  Supplier: Example Energy Co.
  Rate Type: Fixed Rate
  Term Length: 12 months
  Savings: 25.08% savings ($0.0301 per unit)
  Cancellation Fee: $0
  Deposit Required: No

============================================================
```

## Data Storage

Rate history is stored in `/data/previous_rates.json`. Mount this directory as a volume to persist data between container runs.

## Troubleshooting

### No rates found

- Verify your `ELECTRIC_ID` and `GAS_ID` are correct
- Check that the API endpoints are accessible
- Review logs for error messages

### No savings detected

- Ensure `CURRENT_ELECTRIC_PRICE` and `CURRENT_GAS_PRICE` are set correctly
- Lower `NOTIFICATION_THRESHOLD` if you want to see smaller savings
- Check that the price format matches (numeric value, e.g., `0.12` for $0.12/kWh)

### Scheduler not running

- Check logs: `docker logs pa-power-notifier`
- Verify cron is running: `docker exec pa-power-notifier ps aux | grep cron`
- Check cron logs: `docker exec pa-power-notifier cat /data/cron.log`

## License

MIT

