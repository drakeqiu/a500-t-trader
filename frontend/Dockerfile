FROM node:24-slim AS build
COPY --from=oven/bun:1.4.4 /usr/local/bin/bun /usr/local/bin/bun
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile
COPY next.config.ts tsconfig.json ./
COPY src/ ./src/
ARG NEXT_PUBLIC_A500_OIDC_ISSUER=http://localhost:8081/realms/a500
ARG NEXT_PUBLIC_A500_OIDC_CLIENT_ID=a500-device-client
ENV NEXT_PUBLIC_A500_OIDC_ISSUER=$NEXT_PUBLIC_A500_OIDC_ISSUER \
    NEXT_PUBLIC_A500_OIDC_CLIENT_ID=$NEXT_PUBLIC_A500_OIDC_CLIENT_ID \
    NEXT_TELEMETRY_DISABLED=1
RUN bun run build

FROM node:24-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
