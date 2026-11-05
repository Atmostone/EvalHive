# --- Dev stage ---
FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS dev
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "3000"]

# --- Production build ---
FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

# --- Production serve ---
FROM nginx:alpine@sha256:785835ea65db4bba6f021acc0f3be91b0fef76a53fe41f8588c6698d2aed0606 AS prod
COPY --from=build /app/dist /usr/share/nginx/html
# Full reverse-proxy config: serves the SPA + proxies /api and /ws + TLS + ACME.
COPY nginx.prod.conf /etc/nginx/nginx.conf
EXPOSE 80 443
