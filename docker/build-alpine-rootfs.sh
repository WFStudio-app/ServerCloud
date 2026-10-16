#!/usr/bin/env bash
# build-alpine-rootfs.sh — assemble a real bootable Alpine Linux rootfs
# with ServerCloud / TruckOS pre-installed, WITHOUT Docker.
#
# Requires: curl (or wget), gpg optional, running on Linux with sudo.
# Output:   docker/alpine-build/rootfs  +  alpine-servercloud.tar.gz
#
# Usage:
#   ./docker/build-alpine-rootfs.sh              # minimal + full setup
#   ./docker/build-alpine-rootfs.sh --minimal    # just base + python
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BUILD="${HERE}/alpine-build"
ROOTFS="${BUILD}/rootfs"
ARCH="${ARCH:-x86_64}"
ALPINE_RELEASE="${ALPINE_RELEASE:-3.20}"
MIRROR="${MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
MINIMAL=0
[ "${1:-}" = "--minimal" ] && MINIMAL=1

latest_minirootfs() {
    curl -fsSL "${MIRROR}/v${ALPINE_RELEASE}/latest/${ARCH}/" \
        | grep -o 'alpine-minirootfs-[0-9.]*'"-${ARCH}"'.tar.gz' \
        | head -1
}

echo "==> [1/5] Downloading Alpine ${ALPINE_RELEASE} minirootfs (${ARCH})"
mkdir -p "${BUILD}"
TARBALL="$(latest_minirootfs)"
if [ ! -f "${BUILD}/${TARBALL}" ]; then
    curl -fsSL -o "${BUILD}/${TARBALL}" \
        "${MIRROR}/v${ALPINE_RELEASE}/latest/${ARCH}/${TARBALL}"
fi

echo "==> [2/5] Extracting rootfs"
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"
if [ "$(id -u)" -ne 0 ]; then SUDO=sudo; else SUDO=; fi
$SUDO tar -xzf "${BUILD}/${TARBALL}" -C "${ROOTFS}"

echo "==> [3/5] Bootstrapping packages via apk --root"
cat > "${ROOTFS}/etc/apk/repositories" <<EOF
${MIRROR}/v${ALPINE_RELEASE}/main
${MIRROR}/v${ALPINE_RELEASE}/community
EOF
cp /etc/apk/keys/*.pub "${ROOTFS}/etc/apk/keys/" 2>/dev/null || true

PKGS="python3 py3-pip openrc shadow curl git bash"
COMMUNITY_PKGS="community/busybox-initscripts"
[ "$MINIMAL" = 1 ] && PKGS="python3 openrc"
$SUDO apk --root "${ROOTFS}" add --no-cache $PKGS $COMMUNITY_PKGS

echo "==> [4/5] Installing ServerCloud / TruckOS into the rootfs"
$SUDO mkdir -p "${ROOTFS}/opt/servercloud" "${ROOTFS}/var/lib/servercloud" "${ROOTFS}/var/log/servercloud"
$SUDO cp -a "${REPO}/servercloud" "${REPO}/kernel" "${REPO}/truck" \
          "${REPO}/truck_modules" "${REPO}/security" "${REPO}/tests" \
          "${REPO}/scripts" "${REPO}/servercloud_app.py" \
          "${ROOTFS}/opt/servercloud/"
$SUDO cp -a "${HERE}/alpine-rootfs/." "${ROOTFS}/"
$SUDO chmod 755 "${ROOTFS}/usr/local/bin/servercloud-entrypoint" \
                "${ROOTFS}/usr/local/bin/servercloud-sys" \
                "${ROOTFS}/etc/init.d/servercloud"
$SUDO ln -sf /usr/local/bin/servercloud-entrypoint "${ROOTFS}/usr/local/bin/servercloud"
$SUDO chroot "${ROOTFS}" /bin/sh -c "
    adduser -D -h /var/lib/servercloud -s /bin/bash servercloud 2>/dev/null || true
    chown -R servercloud:servercloud /var/lib/servercloud /var/log/servercloud
    rc-update add servercloud default
    echo 'servercloud' > /etc/alpine-release.sc
"

echo "==> [5/5] Packing image"
IMAGE="${HERE}/alpine-servercloud-${ALPINE_RELEASE}-${ARCH}.tar.gz"
$SUDO tar -czf "${IMAGE}" -C "${ROOTFS}" .
echo ""
echo "Done! Alpine rootfs image: ${IMAGE}"
echo "Boot it in QEMU:"
echo "  qemu-img create -f qcow2 sc.img 2G"
echo "  virt-install --name servercloud --memory 1024 --disk sc.img \\"
echo "      --location ${BUILD}/${TARBALL%.*} --extra-args 'console=ttyS0' --graphics none"
echo "Or run commands directly:"
echo "  sudo chroot ${ROOTFS} su - servercloud -c servercloud"
