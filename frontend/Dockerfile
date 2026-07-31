FROM node:22-alpine AS builder

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY frontend/nginx.conf /etc/nginx/nginx.conf.template
COPY frontend/runtime-config.template.js /etc/bsagent/runtime-config.template.js
COPY frontend/start-nginx.sh /usr/local/bin/start-nginx.sh
COPY --from=builder /build/dist /usr/share/nginx/html
RUN chmod 0555 /usr/local/bin/start-nginx.sh

USER nginx
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/start-nginx.sh"]
