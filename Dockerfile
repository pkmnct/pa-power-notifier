# Build stage: install the Puppeteer helper's packages and compile its TypeScript, so
# the runtime image needs neither npm nor tsx/typescript/esbuild.
FROM node:20-bookworm-slim AS browser-build
WORKDIR /browser
ENV PUPPETEER_SKIP_DOWNLOAD=true
COPY browser/package.json browser/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY browser/tsconfig.json ./
COPY browser/src src
RUN npx tsc -p . && npm prune --omit=dev

FROM python:3.11-slim

WORKDIR /app

# Node + Chromium for the Puppeteer helper — Duquesne's site sits behind Imperva, which
# blocks plain HTTP. Debian's chromium is used instead of Puppeteer's bundled Chrome,
# which has no linux-arm64 build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs chromium \
    && rm -rf /var/lib/apt/lists/*
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=browser-build /browser/package.json browser/
COPY --from=browser-build /browser/node_modules browser/node_modules
COPY --from=browser-build /browser/dist browser/dist

COPY *.py ./

# Run as an unprivileged user. Chromium still runs with --no-sandbox: its own sandbox
# needs kernel features Docker's default seccomp profile blocks, and the container is
# the isolation boundary. On Linux hosts, make ./data writable by uid 1000 (or set
# `user:` in compose); Docker Desktop on macOS maps ownership automatically.
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
USER app

VOLUME /data

CMD ["python", "pa_power_notifier.py"]
