#!/bin/sh

set -eu

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
SESSION_ID="${SESSION_ID:-smoke-$(date +%s)-$$}"
ANALYSIS_TIMEOUT_SECONDS="${ANALYSIS_TIMEOUT_SECONDS:-180}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-2}"
REQUIRE_EXTERNAL_VISION="${REQUIRE_EXTERNAL_VISION:-true}"

: "${ACCESS_TOKEN:?必须通过 ACCESS_TOKEN 提供真实 OIDC access token。}"
: "${TEST_IMAGE:?必须通过 TEST_IMAGE 提供本地测试图片路径。}"

command -v curl >/dev/null 2>&1 || {
  echo "缺少 curl。" >&2
  exit 1
}
command -v jq >/dev/null 2>&1 || {
  echo "缺少 jq。" >&2
  exit 1
}
[ -f "$TEST_IMAGE" ] || {
  echo "测试图片不存在：$TEST_IMAGE" >&2
  exit 1
}

TMP_DIR="$(mktemp -d)"
ASSET_ID=""

cleanup() {
  if [ -n "$ASSET_ID" ]; then
    curl --silent --show-error --output /dev/null \
      --request DELETE \
      --header "Authorization: Bearer $ACCESS_TOKEN" \
      "$BASE_URL/api/assets/$ASSET_ID" || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

echo "[1/5] 检查服务就绪状态"
curl --fail --silent --show-error "$BASE_URL/health/ready" | jq -e . >/dev/null

echo "[2/5] 上传测试图片"
UPLOAD_BODY="$TMP_DIR/upload.json"
UPLOAD_STATUS="$(
  curl --silent --show-error \
    --output "$UPLOAD_BODY" \
    --write-out '%{http_code}' \
    --request POST \
    --header "Authorization: Bearer $ACCESS_TOKEN" \
    --form "file=@$TEST_IMAGE" \
    --form "session_id=$SESSION_ID" \
    "$BASE_URL/api/assets/images"
)"
[ "$UPLOAD_STATUS" = "202" ] || {
  echo "图片上传失败，HTTP $UPLOAD_STATUS：$(cat "$UPLOAD_BODY")" >&2
  exit 1
}
ASSET_ID="$(jq -er '.asset_id' "$UPLOAD_BODY")"

echo "[3/5] 等待 OCR/Vision 异步分析"
DEADLINE="$(( $(date +%s) + ANALYSIS_TIMEOUT_SECONDS ))"
while :; do
  ASSET_BODY="$TMP_DIR/asset.json"
  curl --fail --silent --show-error \
    --header "Authorization: Bearer $ACCESS_TOKEN" \
    "$BASE_URL/api/assets/$ASSET_ID" >"$ASSET_BODY"
  ASSET_STATUS="$(jq -er '.status' "$ASSET_BODY")"
  case "$ASSET_STATUS" in
    ready|partial)
      break
      ;;
    failed)
      echo "图片分析失败：$(jq -c '{status, failure_code}' "$ASSET_BODY")" >&2
      exit 1
      ;;
  esac
  [ "$(date +%s)" -lt "$DEADLINE" ] || {
    echo "图片分析等待超时，最后状态：$ASSET_STATUS" >&2
    exit 1
  }
  sleep "$POLL_INTERVAL_SECONDS"
done

ANALYSIS_BODY="$TMP_DIR/analysis.json"
curl --fail --silent --show-error \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  "$BASE_URL/api/assets/$ASSET_ID/analysis" >"$ANALYSIS_BODY"
jq -e '.analysis.summary | strings | length > 0' "$ANALYSIS_BODY" >/dev/null
ANALYSIS_PROVIDER="$(jq -er '.provider' "$ANALYSIS_BODY")"
if [ "$REQUIRE_EXTERNAL_VISION" = "true" ] && [ "$ANALYSIS_PROVIDER" != "ocr_vision_hybrid" ]; then
  echo "外部 Vision 未成功参与分析，实际 provider：$ANALYSIS_PROVIDER" >&2
  exit 1
fi

echo "[4/5] 调用带图片附件的 Agent SSE 链路"
STREAM_BODY="$TMP_DIR/stream.txt"
REQUEST_BODY="$(
  jq -cn \
    --arg message "请从 A 股投研视角分析这张图片，提取关键指标、结论与风险。" \
    --arg asset_id "$ASSET_ID" \
    '{message: $message, attachments: [$asset_id]}'
)"
STREAM_STATUS="$(
  curl --silent --show-error --no-buffer \
    --max-time 180 \
    --output "$STREAM_BODY" \
    --write-out '%{http_code}' \
    --request POST \
    --header "Authorization: Bearer $ACCESS_TOKEN" \
    --header "Accept: text/event-stream" \
    --header "Content-Type: application/json" \
    --data "$REQUEST_BODY" \
    "$BASE_URL/api/sessions/$SESSION_ID/messages/stream"
)"
[ "$STREAM_STATUS" = "200" ] || {
  echo "Agent SSE 调用失败，HTTP $STREAM_STATUS：$(cat "$STREAM_BODY")" >&2
  exit 1
}
grep -F "event: final" "$STREAM_BODY" >/dev/null || {
  echo "Agent SSE 未返回 final 事件：$(cat "$STREAM_BODY")" >&2
  exit 1
}
grep -F 'data: {"ok": true}' "$STREAM_BODY" >/dev/null || {
  echo "Agent SSE 未正常结束：$(cat "$STREAM_BODY")" >&2
  exit 1
}

echo "[5/5] 生产 smoke 通过"
echo "asset_id=$ASSET_ID session_id=$SESSION_ID provider=$ANALYSIS_PROVIDER"
