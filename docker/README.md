# Alpine Linux base for ServerCloud / TruckOS

This directory gives the whole ServerCloud stack its own **Alpine Linux** base.

## 1. Docker image (fastest)

```bash
docker build -t servercloud:alpine -f docker/Dockerfile.alpine .

docker run --rm -it servercloud:alpine                    # TruckOS boot menu
docker run --rm -e SC_MODE=test servercloud:alpine        # full unit-test suite
docker run --rm -e SC_MODE=api -p 8777:8777 servercloud:alpine   # network API
docker run --rm -it -e SC_MODE=shell servercloud:alpine   # plain Alpine shell
```

Inside the container everything lives under standard FHS paths:

| Path | Purpose |
|---|---|
| `/opt/servercloud` | app source (SC_HOME) |
| `/var/lib/servercloud` | persistent data, truck userland (SC_DATA / TOKENPFS_HOME) |
| `/etc/servercloud/servercloud.conf` | environment configuration |
| `/etc/init.d/servercloud` | OpenRC runscript wrapping `truck-init` |
| `/usr/local/bin/servercloud-sys` | host-shell control of TruckOS services |

## 2. Real bootable rootfs (no Docker)

```bash
./docker/build-alpine-rootfs.sh          # needs sudo + Linux
```

Downloads the official Alpine minirootfs, installs packages with
`apk --root`, copies ServerCloud in, enables the OpenRC service and packs
`alpine-servercloud-<ver>-<arch>.tar.gz` ready for QEMU/LXC/WSL or a VM.

## 3. Native install on an existing Alpine box

`scripts/install.sh` already detects `apk` — on Alpine just run:

```bash
curl -fsSL https://raw.githubusercontent.com/WFStudio-app/ServerCloud/main/scripts/install.sh | bash
servercloud
```

## GitHub Actions

`.github/workflows/alpine-tests.yml` runs the whole test suite inside
`alpine:3.20` containers on every push, plus builds the Docker image.
