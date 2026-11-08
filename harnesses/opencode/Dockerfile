ARG BASE_IMAGE=evalhive-environment-workspace:0.1.0
ARG RUNTIME_TOOLS_IMAGE=evalhive-runtime-tools:development
ARG SUPERVISOR_IMAGE=evalhive-supervisor:development
FROM ${RUNTIME_TOOLS_IMAGE} AS runtime-tools
FROM ${SUPERVISOR_IMAGE} AS supervisor-artifact
FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS node-runtime

FROM ${BASE_IMAGE} AS build
ENV npm_config_fetch_timeout=30000 npm_config_fetch_retries=2 npm_config_audit=false npm_config_fund=false
# Environment images supply benchmark dependencies; the connector pins Node.
COPY --from=node-runtime /usr/local/ /usr/local/
WORKDIR /opt/evalhive
COPY sdk/python/src/evalhive_harness/schemas sdk/python/src/evalhive_harness/schemas
COPY sdk/typescript sdk/typescript
RUN --mount=type=cache,id=evalhive-harness-npm,target=/root/.npm npm ci --prefix sdk/typescript && npm run build --prefix sdk/typescript
COPY harnesses/opencode/package.json harnesses/opencode/package-lock.json harnesses/opencode/
RUN --mount=type=cache,id=evalhive-harness-npm,target=/root/.npm npm ci --omit=optional --prefix harnesses/opencode
COPY harnesses/opencode harnesses/opencode
RUN npm run build --prefix harnesses/opencode

FROM build AS test
COPY harnesses/shared harnesses/shared
CMD ["npm", "test", "--prefix", "/opt/evalhive/harnesses/opencode"]

FROM test AS conformance
RUN --network=none npm test --prefix /opt/evalhive/harnesses/opencode \
    && mkdir /conformance \
    && printf 'Native OpenCode connector conformance passed.\n' > /conformance/result.txt

FROM scratch AS conformance-result
COPY --from=conformance /conformance/ /

FROM build AS production-dependencies
RUN npm prune --omit=dev --omit=optional --prefix harnesses/opencode && npm prune --omit=dev --prefix sdk/typescript

FROM production-dependencies AS bundle-build
COPY --from=runtime-tools /usr/bin/patchelf /usr/local/bin/patchelf
COPY --from=supervisor-artifact /evalhive-supervisor /usr/local/bin/evalhive-supervisor
COPY environments/runtime/build-node-bundle.py /tmp/build-node-bundle.py
RUN python /tmp/build-node-bundle.py --package /opt/evalhive/harnesses/opencode --output /bundle \
    --extra native-hooks.mjs --extra package-lock.json \
    --loader-wrapper node_modules/opencode-ai/bin/opencode.exe \
    --loader-wrapper node_modules/opencode-linux-x64-baseline/bin/opencode \
    --probe '{"argv":["/evalhive-runtime/connector/node_modules/opencode-ai/bin/opencode.exe","--version"],"stdout":"1.19.5\n"}'

FROM scratch AS bundle
COPY --from=bundle-build /bundle /bundle

FROM ${BASE_IMAGE} AS runtime
COPY --from=node-runtime /usr/local/ /usr/local/
COPY environments/workspace/time_server.py environments/workspace/web_server.py /app/
COPY --from=production-dependencies /opt/evalhive/sdk/typescript/dist /opt/evalhive/sdk/typescript/dist
COPY --from=production-dependencies /opt/evalhive/sdk/typescript/package.json /opt/evalhive/sdk/typescript/package.json
COPY --from=production-dependencies /opt/evalhive/sdk/typescript/node_modules /opt/evalhive/sdk/typescript/node_modules
COPY --from=production-dependencies /opt/evalhive/harnesses/opencode/dist /opt/evalhive/harnesses/opencode/dist
COPY --from=production-dependencies /opt/evalhive/harnesses/opencode/package.json /opt/evalhive/harnesses/opencode/package-lock.json /opt/evalhive/harnesses/opencode/manifest.json /opt/evalhive/harnesses/opencode/
COPY --from=production-dependencies /opt/evalhive/harnesses/opencode/native-hooks.mjs /opt/evalhive/harnesses/opencode/native-hooks.mjs
COPY --from=production-dependencies /opt/evalhive/harnesses/opencode/node_modules /opt/evalhive/harnesses/opencode/node_modules
RUN ln -s /opt/evalhive/harnesses/opencode /opt/connector
WORKDIR /workspace
ENTRYPOINT []
CMD ["node", "/opt/connector/dist/index.js"]
