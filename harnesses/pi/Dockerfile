ARG BASE_IMAGE=node:22.23.1-bookworm-slim
FROM node:22.23.1-bookworm-slim AS node-runtime

FROM ${BASE_IMAGE} AS build
# Environment images supply benchmark dependencies; the connector pins Node.
COPY --from=node-runtime /usr/local/ /usr/local/
WORKDIR /opt/evalhive
COPY sdk/python/src/evalhive_harness/schemas sdk/python/src/evalhive_harness/schemas
COPY sdk/typescript sdk/typescript
RUN npm ci --prefix sdk/typescript && npm run build --prefix sdk/typescript
COPY harnesses/pi harnesses/pi
RUN npm ci --prefix harnesses/pi && npm run build --prefix harnesses/pi

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
