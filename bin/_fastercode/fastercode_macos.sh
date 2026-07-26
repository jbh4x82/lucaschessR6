#!/bin/bash
# Build FasterCode on macOS (clang). Equivalent of fastercode_linux.sh.
# Usage:  ./fastercode_macos.sh [path-to-python]
# Default python is the repo venv at ../../.venv/bin/python if present.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

PYTHON="${1:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$HERE/../../.venv/bin/python" ]; then
        PYTHON="$HERE/../../.venv/bin/python"
    else
        PYTHON="$(command -v python3)"
    fi
fi

ARCH="$(uname -m)"

# Match the deployment target Python was built against, otherwise the linker
# warns for every object file in libirina.a.
DEPLOY_TARGET="$("$PYTHON" -c \
    'import sysconfig; print(sysconfig.get_config_var("MACOSX_DEPLOYMENT_TARGET") or "")')"
if [ -n "$DEPLOY_TARGET" ]; then
    export MACOSX_DEPLOYMENT_TARGET="$DEPLOY_TARGET"
fi

echo ""
echo ":: Building FasterCode (macOS, $ARCH) with $PYTHON"
echo ""

cd ./src/irina
clang -Wall -O2 -fPIC -fno-strict-aliasing \
    -c lc.c board.c data.c eval.c hash.c loop.c makemove.c movegen.c \
       movegen_piece_to.c search.c util.c pgn.c parser.c polyglot.c -DNDEBUG
ar rcs libirina.a lc.o board.o data.o eval.o hash.o loop.o makemove.o \
    movegen.o movegen_piece_to.o search.o util.o pgn.o parser.o polyglot.o
mv libirina.a ../..

rm -f ./*.o
cd ..

cat Faster_Irina.pyx Faster_Polyglot.pyx > FasterCode.pyx

"$PYTHON" setup_macos.py build_ext --inplace

# LucasR.py does a plain `import FasterCode`, so the module has to sit in bin/
cp -f FasterCode*.so "$HERE/../"

echo ""
echo ":: Building Complete -> $(cd "$HERE/.." && ls FasterCode*.so)"
echo ""
