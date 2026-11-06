"""Scientific QC figures derived from the verified published plot data."""
from functools import lru_cache
from io import BytesIO
import json
from threading import Lock

import numpy as np
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator


KINDS = ('counts', 'genes', 'mitochondrial', 'scatter')
FORMATS = {'svg': 'image/svg+xml', 'png': 'image/png', 'jpeg': 'image/jpeg', 'pdf': 'application/pdf'}
PASS = '#0072B2'
FAIL = '#D55E00'
_LOCK = Lock()


@lru_cache(maxsize=12)
def render_plot(payload: bytes, policy: str, kind: str, format: str, scale: str = 'linear') -> bytes:
    if kind not in KINDS or format not in FORMATS or scale not in ('linear', 'log'):
        raise ValueError('Unsupported plot, file format or scale')
    # Matplotlib's font machinery is shared between requests.
    with _LOCK:
        return _render(payload, policy, kind, format, scale)


def _render(payload, policy, kind, format, scale):
    data, config = json.loads(payload), json.loads(policy)
    passed = np.asarray(data['passing'], dtype=bool)
    fig = Figure(figsize=(8, 5.4), dpi=150, facecolor='white')
    ax = fig.subplots()
    fig.subplots_adjust(left=.12, right=.96, bottom=.19, top=.80)
    for spine in ('top', 'right'):
        ax.spines[spine].set_visible(False)
    for spine in ('left', 'bottom'):
        ax.spines[spine].set_color('#B4C3CC')
    ax.set_axisbelow(True)
    ax.grid(axis='y', color='#E6EDF1', linewidth=.7)
    ax.tick_params(labelsize=10, colors='#425563', length=3)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:,.0f}'))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:,.0f}'))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    ax.xaxis.set_major_locator(MaxNLocator(nbins=6, integer=kind != 'mitochondrial'))
    footer = 'All cells shown. Blue: passes QC. Orange: excluded by the selected QC policy.'
    if kind == 'scatter':
        x, y = np.asarray(data['total_counts']), np.asarray(data['detected_genes'])
        for mask, color, label in ((passed, PASS, 'Pass QC'), (~passed, FAIL, 'Excluded')):
            ax.scatter(x[mask], y[mask], s=13, alpha=.55, color=color, edgecolors='none',
                       label=f'{label} ({int(mask.sum()):,})', rasterized=False)
        ax.set(xlabel='Total counts per cell', ylabel='Detected genes per cell')
        title = 'Counts and detected genes'
        ax.set_xlim(0, max(float(x.max(initial=0)) * 1.05, 1))
        ax.set_ylim(0, max(float(y.max(initial=0)) * 1.05, 1))
        if scale == 'log':
            ax.set_xscale('symlog', linthresh=1)
            ax.set_yscale('symlog', linthresh=1)
    else:
        key, xlabel, title = {
            'counts': ('total_counts', 'Total counts per cell', 'Counts per cell'),
            'genes': ('detected_genes', 'Detected genes per cell', 'Detected genes per cell'),
            'mitochondrial': ('mitochondrial_pct', 'Mitochondrial counts (%)', 'Mitochondrial percentage'),
        }[kind]
        values = np.asarray([np.nan if x is None else x for x in data[key]], dtype=float)
        finite = np.isfinite(values)
        high = max(float(values[finite].max(initial=0)) * 1.05, 1)
        if kind == 'mitochondrial':
            high = min(100, max(high, config['max_mito_pct'] * 1.05))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:g}'))
        if scale == 'log' and kind != 'mitochondrial' and high > 1:
            bins = np.concatenate(([0], np.geomspace(1, high, 40)))
            ax.set_xscale('symlog', linthresh=1)
        else:
            bins = np.linspace(0, high, 41)
        ax.hist([values[finite & passed], values[finite & ~passed]], bins=bins, stacked=True,
                color=[PASS, FAIL], label=['Pass QC', 'Excluded'], linewidth=.3, edgecolor='white')
        ax.set(xlabel=xlabel, ylabel='Number of cells', xlim=(0, high))
        thresholds = {'counts': [], 'genes': [config['min_genes'], config['max_genes']],
                      'mitochondrial': [config['max_mito_pct']]}[kind]
        for i, threshold in enumerate(thresholds):
            ax.axvline(threshold, color='#52616B', linestyle='--', linewidth=1.2,
                       label='QC threshold' if i == 0 else '_nolegend_')
        if thresholds and max(thresholds) > high:
            ax.set_xlim(0, max(thresholds) * 1.03)
        missing = int((~finite).sum())
        if missing:
            footer += f' {missing:,} undefined values omitted.'
    if scale == 'log' and kind != 'mitochondrial':
        for axis in ([ax.xaxis, ax.yaxis] if kind == 'scatter' else [ax.xaxis]):
            upper = axis.get_view_interval()[1]
            ticks = [0] + [10 ** i for i in range(8) if 10 ** i <= upper]
            axis.set_ticks(ticks)
            axis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:,.0f}'))
        footer += '\nLogarithmic count axes; values from 0 to 1 remain linear.'
    ax.set_xlabel(ax.get_xlabel(), fontsize=11, labelpad=10, color='#283F4D')
    ax.set_ylabel(ax.get_ylabel(), fontsize=11, labelpad=10, color='#283F4D')
    fig.text(.12, .93, title, fontsize=17, weight='bold', color='#173C37')
    fig.text(.12, .875, f'{config["species"].capitalize()} | {len(passed):,} cells | '
             f'{int(passed.sum()):,} pass QC', fontsize=10, color='#52616B')
    ax.legend(frameon=False, fontsize=9, loc='lower right', bbox_to_anchor=(1, 1.01),
              ncol=3, borderaxespad=0, handlelength=1.5, columnspacing=1.2)
    fig.text(.12, .035, footer, fontsize=8, color='#52616B', linespacing=1.5)
    out = BytesIO()
    try:
        kwargs = {'pil_kwargs': {'quality': 95}} if format == 'jpeg' else {}
        fig.savefig(out, format=format, dpi=300, facecolor='white', **kwargs)
        return out.getvalue()
    finally:
        fig.clear()
