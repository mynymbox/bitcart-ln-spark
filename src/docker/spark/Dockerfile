FROM node:22-bookworm-slim

WORKDIR /app

ENV NODE_ENV=production
ENV SPARK_BRIDGE_PORT=5055
ENV SPARK_BRIDGE_AUTH_FILE=/auth/secret

COPY package.json ./
RUN npm install --omit=dev --legacy-peer-deps

COPY server.mjs docker-entrypoint.sh ./
RUN chmod 755 docker-entrypoint.sh \
    && mkdir -p /auth \
    && chown node:node /auth

EXPOSE 5055

ENTRYPOINT ["./docker-entrypoint.sh"]
