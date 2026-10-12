# syntax=docker/dockerfile:1
# SANA GTM dashboard: the React build served by nginx, which also proxies /api to the API
# container — one origin, no CORS, no per-restart rebuilds. Build context: the repository root.
FROM node:22-bookworm-slim AS build
WORKDIR /web
COPY cloud/web/package.json cloud/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY cloud/web/ ./
# Public values only: the auth origin and its anon/publishable key (both are visible to every browser).
ARG VITE_SUPABASE_URL
ARG VITE_SUPABASE_ANON_KEY
ENV VITE_AUTH_MODE=supabase VITE_DEPLOY_ENV=production VITE_API_URL=
RUN test -n "$VITE_SUPABASE_URL" && test -n "$VITE_SUPABASE_ANON_KEY" && node scripts/build-selfhost.mjs

FROM nginxinc/nginx-unprivileged:stable-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY deploy/selfhost/web/default.conf.template /etc/nginx/templates/default.conf.template
COPY deploy/selfhost/web/security-headers.conf /etc/nginx/snippets/security-headers.conf
EXPOSE 8080
