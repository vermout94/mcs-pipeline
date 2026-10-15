"""Box plots and run sequence plots as PDF.

The boxes use the quartiles of definition 8 (quantiles.py) and whiskers to
the most extreme value inside the fences at 1.5 IQR, so the points outside
the whiskers are the rows of unusual.csv. Every run is drawn as a point.
The PDF has no creation date, so the same data gives the same file.
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from . import __version__  # noqa: E402
from .analyze import box_stats  # noqa: E402
from .load import Campaign  # noqa: E402
from .provenance import describe  # noqa: E402

# Categorical slots in fixed order, one per configuration (by cell).
COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
CONFIG_MARKERS = ("o", "^", "s", "D", "v", "P", "X", "*")
CPU_MARKERS = ("o", "s", "^", "D", "v", "P", "X", "*")
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#d9d8d4"
LABELS = {"stages_sum": "sum of stages", "job": "job (Jobs API)"}
GOLDEN = 0.6180339887498949

STYLE = {
    "font.size": 7,
    "axes.titlesize": 7,
    "axes.labelsize": 7,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.5,
    "axes.edgecolor": INK_MUTED,
    "axes.labelcolor": INK,
    "axes.titlecolor": INK,
    "xtick.color": INK_MUTED,
    "ytick.color": INK_MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.linewidth": 0.6,
    "pdf.fonttype": 42,
    "figure.facecolor": "white",
}


def _metadata(campaign: Campaign, git: dict, title: str) -> dict:
    commit, dirty = describe(git)
    return {
        "Title": f"{title}, {campaign.id}",
        "Subject": (
            f"campaign {campaign.id}, campaign commit {campaign.repo_commit}, "
            f"analysis commit {commit} (dirty: {dirty})"
        ),
        "Keywords": f"{campaign.id} {campaign.repo_commit} {commit}",
        "Creator": f"mcs_analysis {__version__}",
        "CreationDate": None,
    }


def _grid(campaign: Campaign):
    metrics = campaign.metrics
    ncols = 3
    nrows = math.ceil(len(metrics) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(7.0, 1.85 * nrows + 0.7), squeeze=False)
    for ax in axes.flat[len(metrics):]:
        ax.set_visible(False)
    return fig, list(axes.flat[: len(metrics)])


def _style_axis(ax, metric: str) -> None:
    ax.set_title(f"{LABELS.get(metric, metric)} (s)", loc="left")
    ax.grid(axis="y", color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def _jitter(position: int) -> float:
    return ((position * GOLDEN) % 1.0 - 0.5) * 0.36


def boxplot(path: Path, campaign: Campaign, git: dict) -> None:
    included = campaign.included()
    colors = {c.name: COLORS[i % len(COLORS)] for i, c in enumerate(campaign.configurations)}
    models = sorted({r.cpu_model for r in included if r.cpu_model is not None})
    markers = {m: CPU_MARKERS[i % len(CPU_MARKERS)] for i, m in enumerate(models)}
    with plt.rc_context(STYLE):
        fig, axes = _grid(campaign)
        for ax, metric in zip(axes, campaign.metrics):
            configs = [c.name for c in campaign.configurations
                       if any(r.configuration == c.name and metric in r.metrics for r in included)]
            stats = []
            for name in configs:
                values = [r.metrics[metric] for r in included if r.configuration == name and metric in r.metrics]
                b = box_stats(values)
                stats.append({"med": b["median"], "q1": b["q1"], "q3": b["q3"],
                              "whislo": b["whisker_low"], "whishi": b["whisker_high"],
                              "fliers": [], "label": name})
            positions = list(range(1, len(configs) + 1))
            parts = ax.bxp(stats, positions=positions, widths=0.55, showfliers=False, patch_artist=True,
                           medianprops={"color": INK, "linewidth": 1.0},
                           whiskerprops={"color": INK_MUTED, "linewidth": 0.7},
                           capprops={"color": INK_MUTED, "linewidth": 0.7})
            for patch, name in zip(parts["boxes"], configs):
                patch.set_facecolor(colors[name] + "33")
                patch.set_edgecolor(colors[name])
                patch.set_linewidth(0.8)
            for pos, name in zip(positions, configs):
                for run in (r for r in included if r.configuration == name and metric in r.metrics):
                    ax.scatter(pos + _jitter(run.position), run.metrics[metric], s=13,
                               marker=markers.get(run.cpu_model, "o"), facecolor=colors[name],
                               edgecolor="white", linewidth=0.3, zorder=3)
            _style_axis(ax, metric)
            ax.set_xlim(0.4, len(configs) + 0.6)
        handles = [Line2D([], [], linestyle="none", marker=markers[m], markersize=5,
                          markerfacecolor=INK_MUTED, markeredgecolor="white", label=m) for m in models]
        fig.legend(handles=handles, loc="lower center", ncol=min(len(handles), 3), frameon=False,
                   title="CPU model of the run", title_fontsize=6.5)
        fig.tight_layout(rect=(0, 0.06, 1, 1))
        fig.savefig(path, metadata=_metadata(campaign, git, "Box plots per stage"))
        plt.close(fig)


def sequence(path: Path, campaign: Campaign, git: dict) -> None:
    included = campaign.included()
    styles = {c.name: (COLORS[i % len(COLORS)], CONFIG_MARKERS[i % len(CONFIG_MARKERS)])
              for i, c in enumerate(campaign.configurations)}
    split = campaign.blocks // 2
    late = [r.position for r in campaign.runs if r.block >= split]
    split_x = min(late) - 0.5 if late else None
    with plt.rc_context(STYLE):
        fig, axes = _grid(campaign)
        for ax, metric in zip(axes, campaign.metrics):
            for c in campaign.configurations:
                color, marker = styles[c.name]
                runs = [r for r in included if r.configuration == c.name and metric in r.metrics]
                if not runs:
                    continue
                ax.scatter([r.position for r in runs], [r.metrics[metric] for r in runs], s=13,
                           marker=marker, facecolor=color, edgecolor="white", linewidth=0.3, zorder=3)
                first = [r for r in runs if r.first_runs]
                ax.scatter([r.position for r in first], [r.metrics[metric] for r in first], s=40,
                           marker=marker, facecolor="none", edgecolor=INK, linewidth=0.7, zorder=4)
            if split_x is not None:
                ax.axvline(split_x, color=INK_MUTED, linewidth=0.6, linestyle=(0, (3, 2)))
            _style_axis(ax, metric)
            ax.set_xlim(-1.5, len(campaign.runs) + 0.5)
            ax.set_xlabel("position in the campaign")
        handles = [Line2D([], [], linestyle="none", marker=m, markersize=5, markerfacecolor=col,
                          markeredgecolor="white", label=name) for name, (col, m) in styles.items()]
        handles.append(Line2D([], [], linestyle="none", marker="o", markersize=6, markerfacecolor="none",
                              markeredgecolor=INK, label="first runs"))
        handles.append(Line2D([], [], color=INK_MUTED, linewidth=0.6, linestyle=(0, (3, 2)),
                              label=f"start of block {split}"))
        fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False)
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        fig.savefig(path, metadata=_metadata(campaign, git, "Durations over the position in the campaign"))
        plt.close(fig)


def write_plots(out_dir: Path, campaign: Campaign, git: dict) -> list:
    boxplot(out_dir / "boxplot.pdf", campaign, git)
    sequence(out_dir / "trend.pdf", campaign, git)
    return ["boxplot.pdf", "trend.pdf"]
