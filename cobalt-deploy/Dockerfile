FROM node:24-alpine3.22 AS build
WORKDIR /src
RUN apk add --no-cache git python3 alpine-sdk
RUN corepack enable
RUN git clone --no-checkout --depth 1 https://github.com/Rexarrior/cobalt.git . \
    && git fetch --depth 1 origin 2e8c384defaefa381bbdd51053ee0a7ebca2ee2a \
    && git checkout --detach 2e8c384defaefa381bbdd51053ee0a7ebca2ee2a
RUN git clone --depth 1 https://github.com/imputnet/yt-session-generator.git /src/yt-session-generator
RUN python3 -c "from pathlib import Path; p=Path('/src/api/src/core/env.js'); s=p.read_text(); old='ytSessionReloadInterval: 300,'; new='ytSessionReloadInterval: 30,'; assert old in s; p.write_text(s.replace(old,new))"
RUN pnpm install --prod --frozen-lockfile
RUN pnpm deploy --filter=@imput/cobalt-api --prod /prod/api

FROM node:24-alpine3.22
WORKDIR /app
ENV NODE_ENV=production
RUN apk add --no-cache python3 py3-pip py3-virtualenv chromium xvfb nss freetype harfbuzz ca-certificates ttf-freefont su-exec procps
COPY --from=build --chown=node:node /prod/api /app
COPY --from=build --chown=node:node /src/.git /app/.git
COPY --from=build /src/yt-session-generator /app/yt-session-generator
RUN python3 -m venv /opt/yt-session-venv \
    && python3 -c "from pathlib import Path; p=Path('/app/yt-session-generator/requirements.txt'); s=p.read_text(); old='nodriver==0.32'; new='nodriver==0.50.3'; assert old in s; s=s.replace(old,new); old_ws='websockets==13.1'; new_ws='websockets>=14,<16'; assert old_ws in s; p.write_text(s.replace(old_ws,new_ws))" \
    && /opt/yt-session-venv/bin/pip install --no-cache-dir -r /app/yt-session-generator/requirements.txt \
    && python3 -c "from pathlib import Path; root=Path('/opt/yt-session-venv/lib/python3.12/site-packages/nodriver'); bad=[];\nfor p in root.rglob('*.py'):\n    try: p.read_bytes().decode('utf-8')\n    except UnicodeDecodeError: bad.append(p); [p.write_bytes(p.read_bytes().decode('utf-8','replace').encode('utf-8')) for p in bad]; print(f'[nodriver] normalized non-UTF8 source files={len(bad)}')" \
    && python3 -c "from pathlib import Path; p=Path('/app/yt-session-generator/potoken_generator/server.py'); s=p.read_text(); old=\"'/token': self.get_potoken,\"; new=\"'/token': self.get_potoken,\\n            '/get_pot': self.get_potoken,\"; assert old in s; p.write_text(s.replace(old,new))" \
    && python3 -c "from pathlib import Path; p=Path('/app/yt-session-generator/potoken_generator/extractor.py'); s=p.read_text(); old=\"logger.info(f'new token: {token_info.to_json()}')\"; new=\"logger.info(f'new token generated: visitor_data_length={len(token_info.visitor_data)} po_token_length={len(token_info.potoken)}')\"; assert old in s; p.write_text(s.replace(old,new))" \
    && python3 -c "from pathlib import Path; p=Path('/app/yt-session-generator/potoken_generator/extractor.py'); s=p.read_text(); old=\"browser = await nodriver.start(headless=False,\"; new=\"browser = await nodriver.start(headless=False, sandbox=False,\"; assert old in s; p.write_text(s.replace(old,new))"
COPY --chown=node:node entrypoint.sh /app/entrypoint.sh
RUN chmod 755 /app/entrypoint.sh
EXPOSE 9000
ENTRYPOINT ["/app/entrypoint.sh"]
