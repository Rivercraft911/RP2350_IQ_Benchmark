#!/usr/bin/env python3
"""Plot the BB-capacity frontier for selected operating points (uses frontier.py's model).
Bars: capacity at the integer-divider symbol rate. Markers: capacity at the continuous
compute limit and at the AFE limit. Dashed lines: PV-SPI ingress caps. Writes frontier.png."""
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import frontier as F  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
INK, INK2, MUTED, GRID, AXIS, SURF = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
MEAS, PROJ, CPU, AFE = "#2a78d6", "#86b6ef", "#eb6834", "#1baf7a"


def point(label, mod, rate, eng, part, fec="meas", sys_hz=F.SYS128, measured=False, map_cps=None):
    saved = dict(F.MAP_APSK)
    if map_cps is not None:
        F.MAP_APSK[mod] = map_cps * F.frame_symbols(mod)
    r = F.evaluate(mod, rate, eng, partition=part, fec=fec, sys_hz=sys_hz)
    F.MAP_APSK.clear(); F.MAP_APSK.update(saved)
    cpu_cap = F.bb_rate(mod, rate, r["rs_ab_cpu"] * 1e6) / 1e6
    afe_cap = F.bb_rate(mod, rate, r["rs_e_afe"] * 1e6) / 1e6
    return dict(label=label, bb=r["bb_mbps"], cpu=cpu_cap, afe=afe_cap, measured=measured, rs=r["rs_grid"],
                binding=r["binding"])


ROWS = [
    point("QPSK 5/6, N4, 16 Msym/s (measured)", "QPSK", "5/6", "qpsk_desc_n4", "pipe1", measured=True),
    point("QPSK 9/10, N4, A1 frame-parallel", "QPSK", "9/10", "qpsk_ptr_n4", "fp"),
    point("8PSK 5/6, N2, 8 Msym/s (measured)", "8PSK", "5/6", "psk2plane_n2", "pipe0", measured=True),
    point("8PSK 5/6, N4, B1 table, 1 DMA chain/axis", "8PSK", "5/6", "hw4_L7R1_n4", "fp"),
    point("8PSK 2/3, N4, B1 table, 2 chains/axis", "8PSK", "2/3", "hw4_L7R1_n4_2ch", "fp", fec="bchT"),
    point("8PSK 5/6, N4, B1 table, 2 chains/axis", "8PSK", "5/6", "hw4_L7R1_n4_2ch", "fp", fec="bchT"),
    point("16APSK 5/6, N2, split (128 MHz measured)", "16APSK", "5/6", "apsk16_sw_n2", "pipe1", measured=True),
    point("16APSK 5/6, N2, mapper 8 cyc/sym + PL jobs", "16APSK", "5/6", "apsk16_sw_n2", "bal", fec="bchT",
          map_cps=8.0),
    point("32APSK 5/6, N2, split (128 MHz measured)", "32APSK", "5/6", "apsk32_sw_n2", "pipe1", measured=True),
    point("32APSK 5/6, N2, mapper 4 cyc/sym", "32APSK", "5/6", "apsk32_sw_n2", "fp", fec="bchT", map_cps=4.0),
]


def main():
    fig, ax = plt.subplots(figsize=(10.5, 6.2), dpi=200)
    fig.patch.set_facecolor(SURF)
    ax.set_facecolor(SURF)
    y = list(range(len(ROWS)))[::-1]
    for yi, r in zip(y, ROWS):
        ax.barh(yi, r["bb"], height=0.56, color=MEAS if r["measured"] else PROJ,
                edgecolor=MEAS, linewidth=0.8, zorder=2)
        why = {"e_afe": "AFE", "ab_cpu": "compute", "c_dma_handoff": "DMA hand-off", "f_spi": "ingress"}[r["binding"]]
        ax.text(r["bb"] - 0.5, yi, f"{r['bb']:.1f} Mb/s · {why}", va="center", ha="right", fontsize=7,
                color=SURF if r["measured"] else INK, zorder=4)
        ax.plot([r["cpu"]], [yi], marker="o", ms=6.5, color=CPU, mec=SURF, mew=1.2, zorder=5, ls="none")
        ax.plot([r["afe"]], [yi], marker="D", ms=6, color=AFE, mec=SURF, mew=1.2, zorder=5, ls="none")
    for x, lab, ha, dx in ((F.SPI_CAPS["ext25"] / 1e6, "ingress cap, external\n25 MHz SCK: 24.1", "right", -0.5),
                           (F.SPI_CAPS["onchip32"] / 1e6, "ingress cap, on-chip\n32 MHz SCK: 30.9", "left", 0.5)):
        ax.axvline(x, color=INK2, lw=1.0, ls=(0, (4, 3)), zorder=1.5)
        ax.text(x + dx, len(ROWS) - 0.45, lab, fontsize=7, color=INK2, va="bottom", ha=ha)
    ax.set_yticks(y)
    ax.set_yticklabels([r["label"] for r in ROWS], fontsize=8, color=INK)
    ax.set_xlim(0, 72)
    ax.set_ylim(-0.7, len(ROWS) + 0.35)
    ax.set_xlabel("BB data-field capacity (Mb/s), SYS 128 MHz, normal frames with pilots", fontsize=8.5, color=INK2)
    ax.tick_params(axis="x", colors=MUTED, labelsize=8)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    handles = [
        plt.Rectangle((0, 0), 1, 1, color=MEAS, label="Capacity on the integer clock grid (measured mode)"),
        plt.Rectangle((0, 0), 1, 1, fc=PROJ, ec=MEAS, label="Same, projected by frontier.py (not measured)"),
        plt.Line2D([], [], marker="o", color=CPU, ls="none", ms=6.5, label="Two-core compute limit (continuous)"),
        plt.Line2D([], [], marker="D", color=AFE, ls="none", ms=6, label="AFE limit: 65 MSPS/axis or 20 MHz RF"),
    ]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.30, 0.935), ncol=2, fontsize=7.5, frameon=False,
               labelcolor=INK2)
    fig.suptitle("Where each operating point stops: compute, AFE, DMA or ingress", fontsize=11, color=INK, x=0.01,
                 ha="left", y=0.985)
    fig.text(0.01, 0.01, "Digital model only (frontier.py). Bar = capacity at the grid symbol rate; for ingress-bound rows "
             "the deliverable rate is the dashed cap. No RF, EVM or AFE timing is implied.", fontsize=6.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.02, 1, 0.90))
    out = HERE / "frontier.png"
    fig.savefig(out, facecolor=SURF)
    for r in ROWS:
        print(f"{r['label']:48s} bb {r['bb']:6.2f} cpu {r['cpu']:6.2f} afe {r['afe']:6.2f} Rs {r['rs']:.3f} "
              f"{r['binding']}")
    print("wrote", out)


if __name__ == "__main__":
    main()
