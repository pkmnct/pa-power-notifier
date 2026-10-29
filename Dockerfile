FROM python:3.11-slim

WORKDIR /app

# Node + Chromium for the Puppeteer helper in browser/ — Duquesne's site sits behind
# Imperva, which blocks plain HTTP. Debian's chromium is used instead of Puppeteer's
# bundled Chrome, which has no linux-arm64 build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm chromium \
    && rm -rf /var/lib/apt/lists/*
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# pdfplumber's dependency chain (pdfminer.six) needs no native libs,
# but cryptography (used by some pdfplumber extras) may. Keep build deps
# minimal and remove them after install to keep the image small.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installed in-image: tsx pulls a platform-specific esbuild binary.
COPY browser/package.json browser/package-lock.json browser/
RUN cd browser && npm ci --no-audit --no-fund && npm cache clean --force
COPY browser/tsconfig.json browser/
COPY browser/src browser/src

COPY pa_power_notifier.py .
COPY env_bootstrap.py .
COPY check_report.py .
COPY recommendation_engine.py .
COPY gas_usage_forecast.py .
COPY gas_plan_compare.py .
COPY electric_plan_compare.py .
COPY rate_scrapers.py .
COPY duquesne_light_analyzer.py .
COPY peoples_gas_analyzer.py .
COPY tou_analyzer.py .
COPY usage_archive.py .
COPY browser_fetch.py .

RUN mkdir -p /data

CMD ["python", "pa_power_notifier.py"]
