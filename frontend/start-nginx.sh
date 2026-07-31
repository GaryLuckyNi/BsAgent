#!/bin/sh
set -eu

envsubst '${OIDC_AUTHORITY} ${OIDC_CLIENT_ID} ${OIDC_SCOPE} ${OIDC_REDIRECT_URI} ${OIDC_POST_LOGOUT_REDIRECT_URI}' \
  < /etc/bsagent/runtime-config.template.js \
  > /tmp/runtime-config.js
envsubst '${OIDC_CONNECT_SRC}' \
  < /etc/nginx/nginx.conf.template \
  > /tmp/nginx.conf

exec nginx -c /tmp/nginx.conf -g 'daemon off;'
