"""Rebuild the README's vector diagram and PNG preview; no cloud access."""
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


def main():
    destination = Path(__file__).resolve().parents[1] / 'assets' / 'readme'
    destination.mkdir(parents=True, exist_ok=True)
    # Static badges stay readable offline and do not depend on a badge service.
    badges = [('python', 'Python', '3.11', 46, 34, '#3776ab'),
              ('platforms', 'Tested', 'Windows | Linux | macOS', 43, 158, '#0f766e'),
              ('license', 'License', 'AGPL-3.0-only', 49, 98, '#2563eb')]
    for name, title, value, left, right, color in badges:
        width = left + right
        (destination / f'badge-{name}.svg').write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="22" '
            f'viewBox="0 0 {width} 22" role="img" aria-label="{escape(title)}: {escape(value)}">'
            f'<title>{escape(title)}: {escape(value)}</title>'
            f'<clipPath id="r"><rect width="{width}" height="22" rx="4"/></clipPath>'
            f'<g clip-path="url(#r)"><rect width="{left}" height="22" fill="#334155"/>'
            f'<rect x="{left}" width="{right}" height="22" fill="{color}"/></g>'
            '<g fill="#fff" text-anchor="middle" font-family="Verdana,Arial,sans-serif" font-size="11">'
            f'<text x="{left / 2}" y="15">{escape(title)}</text>'
            f'<text x="{left + right / 2}" y="15">{escape(value)}</text></g></svg>\n',
            encoding='utf-8')
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'svg.fonttype': 'none'})
    fig, ax = plt.subplots(figsize=(14, 11), dpi=160)
    fig.patch.set_facecolor('#f8fafc')
    ax.set(xlim=(0, 14), ylim=(11, 0))
    ax.axis('off')
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)

    def label(x, y, text, size=11, color='#475569', weight='normal', **kwargs):
        kwargs.setdefault('ha', 'center')
        ax.text(x, y, text, fontsize=size, color=color, fontweight=weight,
                va='center', linespacing=1.55, **kwargs)

    def box(x, y, width, height, title, detail, fill='#ffffff', edge='#cbd5e1', size=11):
        ax.add_patch(FancyBboxPatch((x, y), width, height,
                                   boxstyle='round,pad=0.015,rounding_size=0.13',
                                   facecolor=fill, edgecolor=edge, linewidth=1.3))
        label(x + width / 2, y + 0.34, title, size=size + 1, color='#0f172a', weight='bold')
        label(x + width / 2, y + height / 2 + 0.22, detail, size=size)

    def arrow(start, end, color='#64748b'):
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle='-|>',
                                    mutation_scale=17, linewidth=1.6, color=color))

    label(0.4, 0.43, 'TraceCell', 28, '#0f172a', 'bold', ha='left')
    label(13.6, 0.45, 'scRNA-seq knowledge platform', 12, '#0f766e', ha='right')
    label(0.4, 0.88, 'Public human or mouse counts → explicit quality rules → results with evidence',
          13, ha='left')

    box(0.4, 1.4, 4.1, 1.18, '1  Choose a sample',
        'Upload .h5ad or use a public example\nSparse, original counts; one sample', '#eff6ff', '#93c5fd')
    box(4.95, 1.4, 4.1, 1.18, '2  Inspect the file',
        'Check dimensions, counts and annotations\nIdentify available gene columns', '#eff6ff', '#93c5fd')
    box(9.5, 1.4, 4.1, 1.18, '3  Review and submit',
        'Confirm species, mappings and QC rules\nSave a fixed configuration for this run', '#eff6ff', '#93c5fd')
    arrow((4.52, 1.99), (4.92, 1.99))
    arrow((9.07, 1.99), (9.47, 1.99))
    ax.plot([11.55, 11.55, 1.62], [2.6, 3.05, 3.05], color='#64748b', linewidth=1.6)
    arrow((1.62, 3.05), (1.62, 3.83))
    label(7, 3.43, 'PROCESSING  ·  progress and stage status are visible in the app',
          11, '#5b21b6', 'bold')

    stages = [
        ('QC metrics', 'Counts, genes, mito %\nPass / exclusion reasons'),
        ('Load tables', 'Cell and gene metadata\nMatrix stays in files'),
        ('dbt analytics', '6 SQL models\n30 data tests'),
        ('Verify independently', 'Source, rows, analytics\nand exact passing cells'),
        ('Publish evidence', 'Checksummed results\nCompletion marker last'),
    ]
    for i, (title, detail) in enumerate(stages):
        x = 0.4 + i * 2.71
        box(x, 3.86, 2.36, 1.45, title, detail, '#f5f3ff', '#c4b5fd', size=9.8)
        if i < 4:
            arrow((x + 2.38, 4.59), (x + 2.68, 4.59))
    ax.plot([12.42, 12.42, 7], [5.33, 5.64, 5.64], color='#64748b', linewidth=1.6)
    arrow((7, 5.64), (7, 6.08))
    box(0.4, 6.12, 13.2, 1.33, '4  Explore, download and compare',
        'Four QC plots · PNG / JPEG / PDF · report and Run Evidence · same-sample policy comparison\n'
        'Paper QC rules need review and confirmation; an optional model answers cited questions',
        '#ecfdf5', '#6ee7b7', size=11)

    label(7, 7.96, 'SAME SCIENTIFIC CHECKS, TWO RUNTIMES', 11, '#0f766e', 'bold')
    box(0.4, 8.33, 6.4, 1.56, 'Local  ·  no GCP account needed',
        'Python serves the browser UI and FastAPI backend\n'
        'SQLite: run history  ·  local files: inputs and results\n'
        'DuckDB + dbt: metadata tables and SQL checks', '#ffffff', '#99f6e4', size=10.5)
    box(7.2, 8.33, 6.4, 1.56, 'GCP deployment  ·  project owner pays',
        'Cloud Run Jobs + Workflows: processing\n'
        'Firestore: run state  ·  GCS: inputs and results\n'
        'BigQuery + dbt: metadata tables and SQL checks', '#ffffff', '#99f6e4', size=10.5)
    label(7, 10.35, 'Cloud support: Terraform / IAM · Artifact Registry · Logging / Monitoring', 11)
    label(7, 10.75, 'Research QC pilot. No clinical interpretation or cell-type inference.', 10, '#64748b')

    fig.savefig(destination / 'pipeline.svg', facecolor=fig.get_facecolor(),
                metadata={'Title': 'TraceCell data pipeline',
                          'Description': 'Input inspection, QC, SQL checks, independent verification and results in local or GCP runtimes.'})
    fig.savefig(destination / 'pipeline.png', facecolor=fig.get_facecolor())
    plt.close(fig)
    print('Wrote assets/readme/pipeline.svg and pipeline.png')


if __name__ == '__main__':
    main()
