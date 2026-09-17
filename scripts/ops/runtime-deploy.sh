#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
runtime-deploy.sh - submit an immutable hosted runtime deployment

Usage:
  ./scripts/ops/runtime-deploy.sh <target-name> --docker-image IMAGE --docker-tag TAG
  ./scripts/ops/runtime-deploy.sh <target-name> --docker-ref IMAGE@sha256:DIGEST

Required environment:
  CONTROL_PLANE_URL             Durable private deployment API base URL
  CONTROL_PLANE_ADMIN_TOKEN     Deployment API admin token
  INSTANCE_SUBDOMAIN            Explicit hosted target (or LH_TARGET_SUBDOMAIN)

Required release provenance:
  RUNTIME_SOURCE_WORKFLOW, RUNTIME_SOURCE_ORDER, RUNTIME_QUALIFICATION_ID
  RUNTIME_DEPLOYMENT_IDEMPOTENCY_KEY

Image metadata (read from the immutable image when omitted):
  RUNTIME_SOURCE_SHA, RUNTIME_BUILD_IDENTITY (full JSON build identity)
  RUNTIME_SCHEMA_VERSION, RUNTIME_SCHEMA_MIN_READER, RUNTIME_SCHEMA_MAX_READER
Optional: RUNTIME_DEPLOY_TIMEOUT

For an explicit operator-selected image, use make reprovision SUBDOMAIN=... IMAGE=...@sha256:...
That records operator image-metadata qualification, not a canary-pass claim.
USAGE
}

APP_ID=""
TIMEOUT="${RUNTIME_DEPLOY_TIMEOUT:-900}"
DOCKER_IMAGE=""
DOCKER_TAG=""
DOCKER_REF=""

parse_args() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi
  APP_ID="${1:-}"
  if [[ $# -gt 0 ]]; then
    shift
  fi
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --timeout) TIMEOUT="${2:-}"; shift 2 ;;
      --docker-image) DOCKER_IMAGE="${2:-}"; shift 2 ;;
      --docker-tag) DOCKER_TAG="${2:-}"; shift 2 ;;
      --docker-ref) DOCKER_REF="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown argument: $1"; usage >&2; exit 1 ;;
    esac
  done
  if [[ -z "$APP_ID" || ( -z "$DOCKER_REF" && ( -z "$DOCKER_IMAGE" || -z "$DOCKER_TAG" ) ) ]]; then
    echo "target plus either --docker-ref or --docker-image/--docker-tag are required" >&2
    usage >&2
    exit 1
  fi
}

main() {
  parse_args "$@"
  # This script intentionally has no SSH, Docker, Compose, or host mutation
  # capability. The private worker is the only deployment authority.
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  # shellcheck source=../lib/hosted-instance.sh
  . "$root/scripts/lib/hosted-instance.sh"
  lh_hosted_require_env CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN
  INSTANCE_SUBDOMAIN="${INSTANCE_SUBDOMAIN:-${LH_TARGET_SUBDOMAIN:-}}"
  if [[ -z "$INSTANCE_SUBDOMAIN" ]]; then
    echo "INSTANCE_SUBDOMAIN (explicit target) is required" >&2
    exit 1
  fi
  lh_hosted_resolve_instance "$INSTANCE_SUBDOMAIN"
  local image_ref=""
  if [[ -n "$DOCKER_REF" ]]; then
    if [[ ! "$DOCKER_REF" =~ ^.+@sha256:[0-9a-f]{64}$ ]]; then
      echo "--docker-ref must be an immutable IMAGE@sha256:DIGEST" >&2
      exit 1
    fi
    image_ref="$DOCKER_REF"
  elif [[ "$DOCKER_TAG" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    image_ref="${DOCKER_IMAGE}@${DOCKER_TAG}"
  else
    image_ref="${DOCKER_IMAGE}:${DOCKER_TAG}"
    # Resolve the publishing tag before submission. The control plane receives
    # only the immutable digest; it never makes a deployment decision from a
    # mutable registry tag.
    digest="$(docker buildx imagetools inspect "$image_ref" --format '{{.Manifest.Digest}}')"
    if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "Unable to resolve $image_ref to an immutable sha256 digest" >&2
      exit 1
    fi
    image_ref="${DOCKER_IMAGE}@${digest}"
  fi
  # The shared hosted helper inspects this exact digest's OCI config and
  # rejects any caller metadata that does not match its source/schema labels.
  export LH_DEPLOYMENT_SOURCE_SHA="${RUNTIME_SOURCE_SHA:-}"
  export LH_DEPLOYMENT_SCHEMA_VERSION="${RUNTIME_SCHEMA_VERSION:-}"
  export LH_DEPLOYMENT_SCHEMA_MIN_READER="${RUNTIME_SCHEMA_MIN_READER:-}"
  export LH_DEPLOYMENT_SCHEMA_MAX_READER="${RUNTIME_SCHEMA_MAX_READER:-}"
  export LH_DEPLOYMENT_BUILD_IDENTITY="${RUNTIME_BUILD_IDENTITY:-}"
  export LH_DEPLOYMENT_SOURCE_WORKFLOW="${RUNTIME_SOURCE_WORKFLOW:-}"
  export LH_DEPLOYMENT_SOURCE_ORDER="${RUNTIME_SOURCE_ORDER:-}"
  export LH_DEPLOYMENT_QUALIFICATION_ID="${RUNTIME_QUALIFICATION_ID:-}"
  export LH_DEPLOYMENT_REASON="${RUNTIME_DEPLOYMENT_REASON:-${APP_ID} durable release}"
  export LH_HOSTED_REPROVISION_TIMEOUT="$TIMEOUT"
  lh_hosted_reprovision "$LH_INSTANCE_ID" "$image_ref"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'deployment_id=%s\n' "$LH_DEPLOYMENT_ID" >> "$GITHUB_OUTPUT"
  fi
}

main "$@"
