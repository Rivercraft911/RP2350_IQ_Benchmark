"""Compare dvbs2_tables.LDPC (from the ETSI PDF) row by row with three other transcriptions,
fetched by build.sh: gr-dtv (dvb_ldpc_bb_impl.cc), xdsopl/LDPC (dvb_s2_tables.hh) and leansdr
(dvbs2_data.h). Run: python3 reference/dvbs2/crosscheck/check_tables.py"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "build" / "src"
sys.path.insert(0, str(HERE.parent))
from dvbs2_tables import LDPC  # noqa: E402

RATES = ("1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10")


def ints(s):
    return [int(v) for v in re.findall(r"\d+", s)]


def grdtv():
    """ldpc_tab_<r>[NS][rows][cols] = {{count, x...}, ...}; the DVB-T2 variants of 2/3N and 3/5S
    are skipped."""
    src = (SRC / "gnuradio/gr-dtv/lib/dvb/dvb_ldpc_bb_impl.cc").read_text()
    out = {}
    pat = r"ldpc_tab_(\d+)_(\d+)([NS])(_DVB\w+)?\[\d+\]\[\d+\] = \{(.*?)\};"
    for m in re.finditer(pat, src, re.S):
        key = ("normal" if m[3] == "N" else "short", f"{m[1]}/{m[2]}")
        if key[1] not in RATES or (m[4] or "_DVBS2") != "_DVBS2":
            continue
        rows = [ints(r) for r in re.findall(r"\{([^{}]*)\}", m[5])]
        out[key] = [tuple(r[1:1 + r[0]]) for r in rows]
    return out


def xdsopl():
    """struct DVB_S2_TABLE_{B,C}n { DEG[], LEN[], POS[] }: LEN[k] rows of DEG[k] addresses."""
    src = (SRC / "xdsopl_ldpc/dvb_s2_tables.hh").read_text()
    out = {}
    for m in re.finditer(r"struct DVB_S2_TABLE_([BC])(\d+)\s*\{(.*?)\n\};", src, re.S):
        arr = {k: ints(re.search(k + r"\[\] = \{(.*?)\}", m[3], re.S)[1])
               for k in ("DEG", "LEN", "POS")}
        rows, p = [], 0
        for deg, n in zip(arr["DEG"], arr["LEN"]):
            for _ in range(n if deg else 0):
                rows.append(tuple(arr["POS"][p:p + deg]))
                p += deg
        assert p == len(arr["POS"])
        out[("normal" if m[1] == "B" else "short", RATES[int(m[2]) - 1])] = rows
    return out


def leansdr():
    """ldpc_{n,s}f_fec<r> = { q, nrows, { {ncols, {x...}}, ... } }. The encoder uses ncols entries
    of a zero-filled 13-entry array, so a count larger than the list reads zeros."""
    src = (SRC / "leansdr/src/leansdr/dvbs2_data.h").read_text()
    names = {r.replace("/", ""): r for r in RATES}
    out = {}
    pat = r"ldpc_([ns])f_fec(\d+) =\s*\{\s*\d+,\s*\d+,\s*\{(.*?)\}\s*\};"
    for m in re.finditer(pat, src, re.S):
        rows = [tuple((ints(body) + [0] * 13)[:int(n)])
                for n, body in re.findall(r"\{\s*(\d+),\s*\{([^{}]*)\}\}", m[3])]
        out[("normal" if m[1] == "n" else "short", names[m[2]])] = rows
    return out


def compare():
    """{source: {(frame, rate): None if identical else a description}}."""
    res = {}
    for name, tab in (("gr-dtv", grdtv()), ("xdsopl", xdsopl()), ("leansdr", leansdr())):
        res[name] = {}
        for key, rows in LDPC.items():
            ours, other = [tuple(r) for r in rows], tab.get(key)
            if other == ours:
                res[name][key] = None
            elif other is None or len(other) != len(ours):
                res[name][key] = "missing" if other is None else f"{len(other)} rows"
            else:
                diff = [g for g, (a, b) in enumerate(zip(ours, other)) if a != b]
                g = diff[0]
                res[name][key] = (f"rows {diff[:4]}{'...' if len(diff) > 4 else ''} differ; "
                                  f"entries per row from row {g}: "
                                  f"{[len(r) for r in other[g:g + 3]]} ({name}) vs "
                                  f"{[len(r) for r in ours[g:g + 3]]} (dvbs2_tables)")
    return res


if __name__ == "__main__":
    for name, r in compare().items():
        bad = {k: v for k, v in r.items() if v}
        print(f"{name}: {len(r) - len(bad)}/{len(r)} tables identical")
        for k, v in bad.items():
            print(f"  {k[0]} {k[1]}: {v}")
