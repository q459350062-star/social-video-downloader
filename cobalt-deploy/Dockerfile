FROM node:24-alpine AS build
WORKDIR /src
RUN apk add --no-cache git python3 alpine-sdk
RUN corepack enable
RUN git clone --no-checkout --depth 1 https://github.com/Rexarrior/cobalt.git . \
    && git fetch --depth 1 origin 2e8c384defaefa381bbdd51053ee0a7ebca2ee2a \
    && git checkout --detach 2e8c384defaefa381bbdd51053ee0a7ebca2ee2a
RUN pnpm install --prod --frozen-lockfile
RUN pnpm deploy --filter=@imput/cobalt-api --prod /prod/api

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build --chown=node:node /prod/api /app
COPY --from=build --chown=node:node /src/.git /app/.git
COPY --chown=node:node entrypoint.sh /app/entrypoint.sh
RUN chmod 755 /app/entrypoint.sh
USER node
EXPOSE 9000
ENTRYPOINT ["/app/entrypoint.sh"]
