#!/bin/bash
# Rebuild pi5b-camera-shim.so for the SystemCore target.
#
# Needs the SystemCore cross toolchain from the upstream release assets
# (systemcorebeta-aarch64-toolchain.tar.gz, extract anywhere and run its
# relocate-sdk.sh once). Ubuntu's gcc-aarch64-linux-gnu also works — the
# library only uses dlsym/strstr/snprintf — but the release toolchain is the
# exact glibc the image ships (2.42), so prefer it.
#
#   ./build.sh /path/to/systemcorebeta-aarch64-toolchain
set -euo pipefail
cd "$(dirname "$0")"
TC="${1:-}"
if [ -n "$TC" ]; then
    CC="$TC/bin/aarch64-buildroot-linux-gnu-gcc"
else
    CC="$(command -v aarch64-buildroot-linux-gnu-gcc || command -v aarch64-linux-gnu-gcc)"
fi
"$CC" -O2 -Wall -Wextra -fPIC -shared -Wl,-soname,pi5b-camera-shim.so \
    -o pi5b-camera-shim.so pi5b-camera-shim.c -ldl
echo "built pi5b-camera-shim.so with $("$CC" --version | head -1)"
