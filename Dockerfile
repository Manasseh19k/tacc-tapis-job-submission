# syntax=docker/dockerfile:1

#Frontend build stage
FROM node:20-bookworm-slim AS frontend
WORKDIR /app/frontend

# Install dependencies first so this layer is cached until the lockfile changes.
COPY tacc-portal-copilot-app/package.json tacc-portal-copilot-app/package-lock.json ./
RUN npm ci --legacy-peer-deps

# Source, build config.
COPY tacc-portal-copilot-app/app ./app/
COPY tacc-portal-copilot-app/components ./components/
COPY tacc-portal-copilot-app/lib ./lib/
COPY tacc-portal-copilot-app/public ./public/
COPY tacc-portal-copilot-app/proxy.ts ./
COPY tacc-portal-copilot-app/next.config.ts tacc-portal-copilot-app/tsconfig.json \
     tacc-portal-copilot-app/postcss.config.mjs tacc-portal-copilot-app/eslint.config.mjs ./

# Standalone build
RUN npm run build && \
    cp -r .next/static .next/standalone/.next/static && \
    cp -r public .next/standalone/public

# Runtime stage
FROM python:3.13-slim-bookworm AS runtime

# uv drives the backend
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends supervisor libstdc++6 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Backend dependencies cached until pyproject/uv.lock change.
COPY tacc-portal-backend/pyproject.toml tacc-portal-backend/uv.lock tacc-portal-backend/.python-version ./backend/
RUN uv sync --locked --no-install-project --project /app/backend

# Backend source.
COPY tacc-portal-backend/app ./backend/app/

# Frontend standalone output from the build stage.
COPY --from=frontend /app/frontend/.next/standalone ./frontend/.next/standalone/

COPY supervisord.conf /etc/supervisor/supervisord.conf

EXPOSE 3000 8000
CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
