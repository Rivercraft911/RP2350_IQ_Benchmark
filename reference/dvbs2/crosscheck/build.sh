#!/bin/sh
# Fetch two independent DVB-S2 transmitters (and one more LDPC table transcription) at pinned commits
# and build the harnesses used by test_dvbs2.py. Output: reference/dvbs2/crosscheck/build/ (git-ignored).
# Needs git, a C++17 compiler and network access. Usage: sh reference/dvbs2/crosscheck/build.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
B="$HERE/build"
mkdir -p "$B/src" "$B/bin"

GR_COMMIT=aee9fd3f79389c4282a98e8d62c8405c73fd91df   # gnuradio/gnuradio main, 2026-08-28 (gr-dtv)
LS_COMMIT=84c59e1c7a1a79338d5722d63f28640cc9d350f3   # pabr/leansdr branch "work", 2022-12-01
XD_COMMIT=32357d8ad55a6a302c34e093759f0454e45cca56   # xdsopl/LDPC master (dvb_s2_tables.hh only)

fetch() {   # fetch NAME URL COMMIT [sparse paths...]
    name=$1 url=$2 commit=$3
    shift 3
    d="$B/src/$name"
    if [ "$(git -C "$d" rev-parse HEAD 2>/dev/null || true)" = "$commit" ]; then return; fi
    rm -rf "$d"
    git init -q "$d"
    git -C "$d" remote add origin "$url"
    if [ $# -gt 0 ]; then git -C "$d" sparse-checkout set "$@"; fi
    git -C "$d" fetch -q --depth 1 --filter=blob:none origin "$commit"
    git -C "$d" checkout -q FETCH_HEAD
}
fetch gnuradio https://github.com/gnuradio/gnuradio "$GR_COMMIT" \
    gr-dtv/lib/dvb gr-dtv/lib/dvbs2 gr-dtv/include/gnuradio/dtv
fetch leansdr https://github.com/pabr/leansdr "$LS_COMMIT"
fetch xdsopl_ldpc https://github.com/xdsopl/LDPC "$XD_COMMIT"

# gr-dtv block implementations, unmodified, against the runtime stubs in gr_stub/.
GR="$B/src/gnuradio/gr-dtv"
c++ -std=c++17 -O2 -w -I"$HERE/gr_stub" -I"$GR/include" -I"$GR/lib" "$HERE/grdtv_tx.cc" \
    "$GR"/lib/dvb/dvb_bbheader_bb_impl.cc "$GR"/lib/dvb/dvb_bbscrambler_bb_impl.cc \
    "$GR"/lib/dvb/dvb_bch_bb_impl.cc "$GR"/lib/dvb/dvb_ldpc_bb_impl.cc \
    "$GR"/lib/dvbs2/dvbs2_interleaver_bb_impl.cc "$GR"/lib/dvbs2/dvbs2_modulator_bc_impl.cc \
    "$GR"/lib/dvbs2/dvbs2_physical_cc_impl.cc -o "$B/bin/grdtv_tx"

# leansdr is written for GCC on Linux. A copy is patched for clang: the VLA initializer in the BCH
# decoder (bch.h, Berlekamp-Massey) becomes a loop, and on macOS exp10f (soft-decision LUT noise model)
# and F_SETPIPE_SZ (FEC decoder helper process) are supplied by -D. None of this is on the TX path.
rm -rf "$B/leansdr_patched"
cp -R "$B/src/leansdr/src" "$B/leansdr_patched"
sed -i.orig 's/TGF C\[NN\]={1,}, B\[NN\]={1,};/TGF C[NN], B[NN]; for ( int i_=0; i_<NN; ++i_ ) C[i_] = B[i_] = 0; C[0] = B[0] = 1;/' \
    "$B/leansdr_patched/leansdr/bch.h"
grep -q 'C\[0\] = B\[0\] = 1' "$B/leansdr_patched/leansdr/bch.h"
EXTRA=""
if [ "$(uname)" = Darwin ]; then EXTRA="-Dexp10f(x)=powf(10.0f,(x)) -DF_SETPIPE_SZ=1031"; fi
# shellcheck disable=SC2086
c++ -std=c++11 -O2 -w $EXTRA -I"$B/leansdr_patched" "$HERE/leansdr_tx.cc" -o "$B/bin/leansdr_tx"

c++ --version | head -1 > "$B/compiler.txt"
echo "built $B/bin/grdtv_tx $B/bin/leansdr_tx"
