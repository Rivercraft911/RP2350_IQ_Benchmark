"""Figure style: Rosé Pine Moon (rose-pine-moon.mplstyle, h4pZ/rose-pine-matplotlib, MIT) made
minimal with matplotx.styles.duftify, set in Source Sans 3 (bundled, OFL)."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotx  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).parent
# Rosé Pine Moon roles
BASE, SURFACE, OVERLAY, HIGH = "#232136", "#2a273f", "#393552", "#44415a"
MUTED, SUBTLE, TEXT = "#6e6a86", "#908caa", "#e0def4"
LOVE, GOLD, ROSE, PINE, FOAM, IRIS = "#eb6f92", "#f6c177", "#ea9a97", "#3e8fb0", "#9ccfd8", "#c4a7e7"

for f in sorted((HERE / "fonts").glob("SourceSans3-*.ttf")):
    fm.fontManager.addfont(str(f))

_rp = dict(matplotlib.rc_params_from_file(str(HERE / "rose-pine-moon.mplstyle"), use_default_template=False))
plt.style.use(matplotx.styles.duftify(_rp))
plt.rcParams.update({
    "font.family": "Source Sans 3", "font.size": 10.5, "text.color": TEXT,
    "axes.titlesize": 13, "axes.titleweight": "semibold", "axes.titlelocation": "left",
    "axes.titlepad": 12, "axes.titlecolor": TEXT, "axes.labelcolor": SUBTLE, "axes.labelsize": 10,
    "axes.labelpad": 7, "axes.facecolor": BASE, "axes.xmargin": 0.02, "axes.ymargin": 0.02,
    "axes.prop_cycle": matplotlib.cycler(color=[FOAM, IRIS, GOLD, ROSE, PINE, LOVE]),
    "xtick.color": SUBTLE, "ytick.color": SUBTLE, "xtick.labelsize": 9.5, "ytick.labelsize": 9.5,
    "xtick.major.size": 0, "ytick.major.size": 0, "xtick.major.pad": 6, "ytick.major.pad": 6,
    "grid.color": OVERLAY, "grid.linewidth": 0.8,
    "figure.facecolor": BASE, "savefig.facecolor": BASE, "figure.dpi": 110, "savefig.dpi": 300,
    "savefig.bbox": "tight", "savefig.pad_inches": 0.3,
    "legend.frameon": False, "legend.fontsize": 9.5, "legend.labelcolor": SUBTLE,
    "lines.linewidth": 2.0, "lines.markersize": 7.5, "lines.markeredgewidth": 1.6,
    "lines.markeredgecolor": BASE, "lines.solid_capstyle": "round",
})

WIDTH = 7.0


def dots(ax, x, y, color, label=None, **kw):
    """Dot-and-line series: a ringed dot per measurement."""
    return ax.plot(x, y, "o-", color=color, label=label, **kw)


def lollipop(ax, y, x, color, fmt="{:.0f} %", size=8.0):
    """Horizontal dot chart row: thin stem from 0, dot at x, value to the right."""
    ax.hlines(y, 0, x, color=color, lw=1.6, alpha=0.55, capstyle="round")
    ax.plot([x], [y], "o", color=color, ms=size)
    ax.annotate(fmt.format(x), (x, y), xytext=(9, 0), textcoords="offset points", va="center",
                fontsize=9.5, color=TEXT)


__all__ = ["plt", "np", "matplotx", "WIDTH", "dots", "lollipop", "BASE", "SURFACE", "OVERLAY",
           "HIGH", "MUTED", "SUBTLE", "TEXT", "LOVE", "GOLD", "ROSE", "PINE", "FOAM", "IRIS"]
