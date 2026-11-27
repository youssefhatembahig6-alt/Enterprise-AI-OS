# syntax=docker/dockerfile:1
# Build context: repository root (npm workspaces need the root lockfile).
FROM node:22.19.0-alpine3.22 AS build
WORKDIR /repo
COPY package.json package-lock.json tsconfig.base.json ./
COPY apps/web/package.json apps/web/
COPY packages/ui/package.json packages/ui/
COPY apps/desktop/package.json apps/desktop/
# The desktop workspace's Electron binary is not needed to build the web app.
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
RUN npm ci --no-audit --no-fund
COPY packages/ui packages/ui
COPY apps/web apps/web
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
# The CSP's connect-src comes from the same build argument as the bundle's API address
# (bug B1). It is validated before the bundle is built; an invalid or over-broad value
# fails here, and the error names the reason without echoing the value.
# The script reads VITE_API_BASE_URL from the environment (set above); keeping it off
# this line keeps the value out of the build log.
RUN node apps/web/render-nginx.mjs apps/web/nginx.conf.template > /repo/nginx.conf
RUN npm run build -w @eaios/web

FROM nginx:1.28.0-alpine
COPY --from=build /repo/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
RUN nginx -t
EXPOSE 3000
