#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-provision}"

log() {
  printf '[runner-vm-host] %s\n' "$*" >&2
}

usage() {
  cat <<'USAGE'
Usage:
  scripts/runner-vm-canary-host.sh provision
  scripts/runner-vm-canary-host.sh reinstall
  scripts/runner-vm-canary-host.sh destroy

Environment (provision/reinstall):
  VM_NAME                  Required guest/runner name
  ENROLL_TOKEN             Required one-time enroll token
  LONGHOUSE_URL            Required Longhouse instance URL

Environment (optional):
  RUNNER_INSTALL_MODE      desktop|server (default: server)
  VM_RELEASE               Ubuntu release alias (default: noble)
  VM_MEMORY_MB             Guest memory in MB (default: 2048)
  VM_CPU                   vCPU count (default: 2)
  VM_DISK_GB               Disk size in GB (default: 10)
  VM_WAIT_TIMEOUT          Seconds to wait for SSH (default: 300)
  RUNNER_VM_GUEST_ARCH     Override amd64|arm64 guest arch
  RUNNER_VM_TMPDIR         Disk-backed temp dir for uvtool sync
  RUNNER_VM_SSH_PUB        SSH public key injected into guest
  RUNNER_VM_SSH_PRIV       SSH private key used for guest SSH/wait
  KEEP_VM                  Keep failed VM for debugging (default: 0)
USAGE
}

require_env() {
  local name=""
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      printf 'Missing required environment variable: %s\n' "$name" >&2
      exit 1
    fi
  done
}

current_user() {
  if [[ -n "${SUDO_USER:-}" ]]; then
    printf '%s\n' "$SUDO_USER"
  else
    id -un
  fi
}

home_dir_for_user() {
  local user="$1"
  getent passwd "$user" | cut -d: -f6
}

host_arch_to_guest_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *)
      printf 'Unsupported host architecture: %s\n' "$(uname -m)" >&2
      exit 1
      ;;
  esac
}

qemu_packages_for_guest_arch() {
  case "$1" in
    amd64) printf 'qemu-system-x86 qemu-utils\n' ;;
    arm64) printf 'qemu-system-arm qemu-efi-aarch64 qemu-utils\n' ;;
    *)
      printf 'Unsupported guest architecture: %s\n' "$1" >&2
      exit 1
      ;;
  esac
}

ensure_host_packages() {
  local guest_arch="$1"
  local packages=(uvtool uvtool-libvirt libvirt-daemon-system libvirt-clients cloud-image-utils)
  local extra_packages
  extra_packages="$(qemu_packages_for_guest_arch "$guest_arch")"
  local pkg=""
  local missing=()

  for pkg in "${packages[@]}" $extra_packages; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    log "Installing host packages: ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
  fi
}

ensure_libvirt_network() {
  sudo systemctl enable --now libvirtd
  sudo virsh net-autostart default >/dev/null 2>&1 || true
  sudo virsh net-start default >/dev/null 2>&1 || true
}

ensure_disk_backed_tmpdir() {
  sudo install -d -m 1777 "$RUNNER_VM_TMPDIR"
}

sync_cloud_image() {
  local guest_arch="$1"
  log "Syncing Ubuntu ${VM_RELEASE} cloud image (${guest_arch})"
  sudo env TMPDIR="$RUNNER_VM_TMPDIR" uvt-simplestreams-libvirt sync release="$VM_RELEASE" arch="$guest_arch"
}

vm_exists() {
  sudo virsh dominfo "$VM_NAME" >/dev/null 2>&1
}

vm_ip() {
  sudo uvt-kvm ip "$VM_NAME"
}

ensure_vm_exists() {
  if ! vm_exists; then
    printf 'VM does not exist: %s\n' "$VM_NAME" >&2
    exit 1
  fi
}

guest_ssh() {
  local ip="$1"
  shift
  ssh \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -i "$RUNNER_VM_SSH_PRIV" \
    ubuntu@"$ip" "$@"
}

wait_for_guest() {
  sudo uvt-kvm wait \
    "$VM_NAME" \
    --insecure \
    --timeout "$VM_WAIT_TIMEOUT" \
    --ssh-private-key-file "$RUNNER_VM_SSH_PRIV"
}

destroy_vm() {
  if vm_exists; then
    log "Destroying VM $VM_NAME"
    sudo uvt-kvm destroy "$VM_NAME"
  fi
}

provision_vm() {
  local guest_arch="$1"
  if vm_exists; then
    printf 'VM already exists: %s\n' "$VM_NAME" >&2
    exit 1
  fi

  log "Creating VM $VM_NAME (${guest_arch})"
  sudo uvt-kvm create \
    "$VM_NAME" \
    release="$VM_RELEASE" \
    arch="$guest_arch" \
    --memory "$VM_MEMORY_MB" \
    --cpu "$VM_CPU" \
    --disk "$VM_DISK_GB" \
    --ssh-public-key-file "$RUNNER_VM_SSH_PUB"
}

install_runner_in_guest() {
  local ip="$1"
  local install_url="${LONGHOUSE_URL%/}/api/runners/install.sh?enroll_token=${ENROLL_TOKEN}&runner_name=${VM_NAME}&mode=${RUNNER_INSTALL_MODE}"

  guest_ssh "$ip" "set -euo pipefail
sudo hostnamectl set-hostname '$VM_NAME'
if ! command -v curl >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y curl ca-certificates
fi
curl -fsSL '$install_url' | sudo bash
sudo systemctl is-active --quiet longhouse-runner
"
}

reboot_and_verify_guest() {
  local ip="$1"
  log "Rebooting $VM_NAME"
  sudo virsh reboot "$VM_NAME" >/dev/null
  wait_for_guest
  ip="$(vm_ip)"
  guest_ssh "$ip" "set -euo pipefail
sudo systemctl is-active --quiet longhouse-runner
test \"\$(hostname -s)\" = '$VM_NAME'
" >/dev/null
  printf '%s\n' "$ip"
}

emit_summary() {
  local ip="$1"
  printf 'VM_NAME=%s\n' "$VM_NAME"
  printf 'VM_IP=%s\n' "$ip"
  printf 'GUEST_ARCH=%s\n' "$GUEST_ARCH"
  printf 'RUNNER_INSTALL_MODE=%s\n' "$RUNNER_INSTALL_MODE"
}

run_install_cycle() {
  ensure_vm_exists
  wait_for_guest
  local ip
  ip="$(vm_ip)"
  log "Guest reachable at $ip"
  install_runner_in_guest "$ip"
  ip="$(reboot_and_verify_guest "$ip")"
  emit_summary "$ip"
}

VM_RELEASE="${VM_RELEASE:-noble}"
VM_MEMORY_MB="${VM_MEMORY_MB:-2048}"
VM_CPU="${VM_CPU:-2}"
VM_DISK_GB="${VM_DISK_GB:-10}"
VM_WAIT_TIMEOUT="${VM_WAIT_TIMEOUT:-300}"
RUNNER_INSTALL_MODE="${RUNNER_INSTALL_MODE:-server}"
KEEP_VM="${KEEP_VM:-0}"
INSTALL_USER="$(current_user)"
INSTALL_HOME="$(home_dir_for_user "$INSTALL_USER")"
RUNNER_VM_SSH_PUB="${RUNNER_VM_SSH_PUB:-$INSTALL_HOME/.ssh/rosetta.pub}"
RUNNER_VM_SSH_PRIV="${RUNNER_VM_SSH_PRIV:-$INSTALL_HOME/.ssh/rosetta}"
RUNNER_VM_TMPDIR="${RUNNER_VM_TMPDIR:-/var/lib/longhouse-vm/tmp}"
GUEST_ARCH="${RUNNER_VM_GUEST_ARCH:-$(host_arch_to_guest_arch)}"
created=0
success=0

cleanup() {
  local status=$?
  if [[ "$ACTION" == "provision" && "$created" == "1" && "$success" != "1" && "$KEEP_VM" != "1" ]]; then
    destroy_vm || true
  fi
  exit "$status"
}
trap cleanup EXIT

case "$ACTION" in
  provision)
    require_env VM_NAME ENROLL_TOKEN LONGHOUSE_URL RUNNER_VM_SSH_PUB RUNNER_VM_SSH_PRIV
    ensure_host_packages "$GUEST_ARCH"
    ensure_disk_backed_tmpdir
    ensure_libvirt_network
    sync_cloud_image "$GUEST_ARCH"
    provision_vm "$GUEST_ARCH"
    created=1
    run_install_cycle
    success=1
    ;;
  reinstall)
    require_env VM_NAME ENROLL_TOKEN LONGHOUSE_URL RUNNER_VM_SSH_PRIV
    ensure_host_packages "$GUEST_ARCH"
    ensure_disk_backed_tmpdir
    ensure_libvirt_network
    run_install_cycle
    success=1
    ;;
  destroy)
    require_env VM_NAME
    destroy_vm
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
