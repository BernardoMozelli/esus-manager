FROM python:3.11-slim

WORKDIR /app

# Dependências do sistema + Docker CLI + inotify-tools (hot-reload) + Playwright deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client curl ca-certificates inotify-tools \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Dependências Python (incluindo watchdog)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium \
    && playwright install-deps chromium \
    && chmod +x /usr/local/lib/python3.11/site-packages/playwright/driver/node

# Copia entrypoint
COPY backend/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Código copiado como fallback — os volumes sobrescrevem em runtime
COPY backend/app.py .
COPY backend/sync_script.py .
COPY frontend/ frontend/

VOLUME ["/app/data"]

ENV FLASK_ENV=production
ENV DB_PATH=/app/data/config.db

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
