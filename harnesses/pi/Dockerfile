ARG BASE_IMAGE=node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS node-runtime

FROM ${BASE_IMAGE} AS build
ENV npm_config_fetch_timeout=30000 npm_config_fetch_retries=2 npm_config_audit=false npm_config_fund=false
# Environment images supply benchmark dependencies; the connector pins Node.
COPY --from=node-runtime /usr/local/ /usr/local/
WORKDIR /opt/evalhive
COPY sdk/python/src/evalhive_harness/schemas sdk/python/src/evalhive_harness/schemas
COPY sdk/typescript sdk/typescript
RUN --mount=type=cache,id=evalhive-harness-npm,target=/root/.npm npm ci --prefix sdk/typescript && npm run build --prefix sdk/typescript
COPY harnesses/pi/package.json harnesses/pi/package-lock.json harnesses/pi/
RUN --mount=type=cache,id=evalhive-harness-npm,target=/root/.npm npm ci --prefix harnesses/pi
COPY harnesses/pi harnesses/pi
RUN npm run build --prefix harnesses/pi

FROM build AS test
COPY harnesses/shared harnesses/shared
CMD ["npm", "test", "--prefix", "/opt/evalhive/harnesses/pi"]

FROM build AS production-dependencies
RUN npm prune --omit=dev --prefix harnesses/pi && npm prune --omit=dev --prefix sdk/typescript

FROM ${BASE_IMAGE} AS runtime
COPY --from=node-runtime /usr/local/ /usr/local/
COPY --from=production-dependencies /opt/evalhive/sdk/typescript/dist /opt/evalhive/sdk/typescript/dist
COPY --from=production-dependencies /opt/evalhive/sdk/typescript/package.json /opt/evalhive/sdk/typescript/package.json
COPY --from=production-dependencies /opt/evalhive/sdk/typescript/node_modules /opt/evalhive/sdk/typescript/node_modules
COPY --from=production-dependencies /opt/evalhive/harnesses/pi/dist /opt/evalhive/harnesses/pi/dist
COPY --from=production-dependencies /opt/evalhive/harnesses/pi/package.json /opt/evalhive/harnesses/pi/manifest.json /opt/evalhive/harnesses/pi/
COPY --from=production-dependencies /opt/evalhive/harnesses/pi/node_modules /opt/evalhive/harnesses/pi/node_modules
RUN ln -s /opt/evalhive/harnesses/pi /opt/connector
WORKDIR /workspace
ENTRYPOINT []
CMD ["node", "/opt/connector/dist/index.js"]
