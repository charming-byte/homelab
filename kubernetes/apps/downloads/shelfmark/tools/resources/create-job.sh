#!/usr/bin/env bash
# Invoked by the adnanh webhook after HMAC verification.
# Args (all base64): $1 source_path, $2 title, $3 author.
# Renders /config/job.yaml and creates the Job via the Kubernetes API.
set -euo pipefail

API="https://kubernetes.default.svc"
SA="/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE="downloads"
TEMPLATE="/config/job.yaml"

SRC_B64="${1:-}"
TITLE_B64="${2:-}"
AUTHOR_B64="${3:-}"

is_b64() { printf '%s' "${1:-}" | grep -Eq '^[A-Za-z0-9+/]*={0,2}$'; }

for value in "$SRC_B64" "$TITLE_B64" "$AUTHOR_B64"; do
  is_b64 "$value" || { echo "invalid base64 argument" >&2; exit 1; }
done
[ -n "$SRC_B64" ] || { echo "empty source_path" >&2; exit 1; }

SRC="$(printf '%s' "$SRC_B64" | base64 -d)"
case "$SRC" in
  /media/media/*) ;;
  *) echo "source path not under /media/media: ${SRC}" >&2; exit 1 ;;
esac
case "$SRC" in
  *..* | *$'\n'*) echo "source path rejected: ${SRC}" >&2; exit 1 ;;
esac

SUFFIX="$(tr -dc 'a-z0-9' < /dev/urandom | head -c 8)"

MANIFEST="$(sed \
  -e "s|__SUFFIX__|${SUFFIX}|g" \
  -e "s|__SOURCE_PATH_B64__|${SRC_B64}|g" \
  -e "s|__TITLE_B64__|${TITLE_B64}|g" \
  -e "s|__AUTHOR_B64__|${AUTHOR_B64}|g" \
  "$TEMPLATE")"

echo "creating Job audiobook-m4b-${SUFFIX} for ${SRC}" >&2
printf '%s' "$MANIFEST" | curl --fail-with-body -sS \
  --cacert "${SA}/ca.crt" \
  -H "Authorization: Bearer $(cat "${SA}/token")" \
  -H 'Content-Type: application/yaml' \
  -X POST --data-binary @- \
  "${API}/apis/batch/v1/namespaces/${NAMESPACE}/jobs"
