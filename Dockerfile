FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
WORKDIR /app
COPY requirements.txt package*.json ./
RUN pip install --no-cache-dir -r requirements.txt && npm ci --omit=dev
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN npx playwright install --with-deps chromium && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
COPY backend/ ./backend/
COPY data/fixtures/ ./data/fixtures/
COPY data/examples/ ./data/examples/
COPY --from=frontend /src/dist ./frontend/dist
RUN useradd --uid 10001 --create-home workmate && chmod -R a+rX /opt/browsers /app
USER workmate
ENV WORKMATE_CLOUD_DATA=/tmp/workmate-sessions WORKMATE_MAX_VISITORS=8 PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["sh", "-c", "python -m uvicorn backend.cloud:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --no-access-log"]
