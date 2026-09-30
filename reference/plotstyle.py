"""Dark theme shared by the figures: JetBrains Mono, hairline grid, a small neon palette."""
import glob
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BG, PANEL, FG, MUTED, GRID = "#0d1117", "#0d1117", "#e6edf3", "#8b949e", "#21262d"
CYAN, VIOLET, PINK, AMBER, GREEN, ROSE = "#38bdf8", "#a78bfa", "#f472b6", "#fbbf24", "#34d399", "#fb7185"
PALETTE = [CYAN, VIOLET, PINK, AMBER, GREEN]

for f in glob.glob(os.path.expanduser("~/Library/Fonts/JetBrainsMono*NerdFont-*.ttf")):
    fm.fontManager.addfont(f)
_font = "JetBrainsMono NF" if any(f.name == "JetBrainsMono NF" for f in fm.fontManager.ttflist) else "monospace"

plt.rcParams.update({
    "font.family": _font, "font.size": 9, "text.color": FG,
    "figure.facecolor": BG, "axes.facecolor": PANEL, "savefig.facecolor": BG,
    "figure.dpi": 110, "savefig.dpi": 160, "savefig.bbox": "tight", "savefig.pad_inches": 0.25,
    "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "axes.titlecolor": FG,
    "axes.titlesize": 11, "axes.titleweight": "medium", "axes.titlelocation": "left", "axes.titlepad": 12,
    "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.7,
    "axes.axisbelow": True, "axes.prop_cycle": matplotlib.cycler(color=PALETTE),
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.major.size": 0, "ytick.major.size": 0,
    "xtick.minor.size": 0, "ytick.minor.size": 0,
    "xtick.major.pad": 6, "ytick.major.pad": 6,
    "legend.frameon": False, "legend.fontsize": 8, "legend.labelcolor": MUTED,
    "lines.linewidth": 1.4, "lines.solid_capstyle": "round",
})


def bar_values(ax, bars, fmt="{:.0f}", inside=False):
    """Small value labels at the bar ends."""
    for b in bars:
        h = b.get_height()
        if not h:
            continue
        y = b.get_y() + (h / 2 if inside else h)
        ax.annotate(fmt.format(h), (b.get_x() + b.get_width() / 2, y), xytext=(0, 0 if inside else 3),
                    textcoords="offset points", ha="center", va="center" if inside else "bottom",
                    fontsize=7.5, color=BG if inside else MUTED,
                    bbox=None if inside else dict(fc=BG, ec="none", pad=0.6))


def glow(ax, x, y, color, lw=1.2, label=None):
    """Line with a soft glow: a few wide low-alpha strokes under the line."""
    for w, a in ((6, 0.05), (3.5, 0.08), (2, 0.12)):
        ax.plot(x, y, color=color, lw=w, alpha=a, solid_capstyle="round")
    ax.plot(x, y, color=color, lw=lw, label=label)


def budget(ax, y, text, color=ROSE):
    ax.axhline(y, color=color, lw=0.9, ls=(0, (4, 3)))
    ax.annotate(text, (1, y), xycoords=("axes fraction", "data"), xytext=(0, 4), textcoords="offset points",
                ha="right", va="bottom", fontsize=7.5, color=color)


__all__ = ["plt", "np", "bar_values", "glow", "budget", "BG", "FG", "MUTED", "GRID",
           "CYAN", "VIOLET", "PINK", "AMBER", "GREEN", "ROSE", "PALETTE"]
