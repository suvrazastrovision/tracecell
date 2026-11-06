"""Private papers and explicitly confirmed, reproducible QC reference comparisons."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal
import uuid

import numpy as np
import psutil
from pydantic import Field, model_validator

from .blobs import encode
from .contracts import Strict, now
from .service import valid_id
from .worker import object_read, read_package

MAX_PDF = 100 * 1000 * 1000  # 100 MB, matching the browser upload limit.
_PARSER = threading.BoundedSemaphore(1)


class PaperRule(Strict):
    field: Literal['min_genes', 'max_genes', 'max_mito_pct']
    value: float = Field(ge=0, le=40001)
    operator: Literal['>=', '>', '<', '<=']
    page: int = Field(ge=1, le=40)
    quote: str = Field(min_length=10, max_length=1000)

    @model_validator(mode='after')
    def rule_bounds(self):
        if self.field == 'max_mito_pct' and self.value > 100:
            raise ValueError('Mitochondrial percentage must be between 0 and 100')
        if self.field != 'max_mito_pct' and not self.value.is_integer():
            raise ValueError('Detected gene thresholds must be whole numbers')
        if self.field == 'min_genes' and self.operator not in ('>', '>='):
            raise ValueError('Minimum genes needs > or >=')
        if self.field != 'min_genes' and self.operator not in ('<', '<='):
            raise ValueError('Maximum thresholds need < or <=')
        return self


class Reference(Strict):
    rules: list[PaperRule] = Field(min_length=1, max_length=3)
    species: Literal['human', 'mouse', 'unspecified']
    tissue: str = Field(min_length=1, max_length=200)
    confirmed: Literal[True]

    @model_validator(mode='after')
    def distinct(self):
        if len({r.field for r in self.rules}) != len(self.rules):
            raise ValueError('Use one rule per QC measure')
        return self


def parse_pdf(data):
    if len(data) > MAX_PDF or not data.startswith(b'%PDF-'):
        raise ValueError('Upload a valid PDF no larger than 100 MB')
    if not _PARSER.acquire(blocking=False):
        raise ValueError('Another paper is being read. Please retry shortly')
    try:
        with tempfile.TemporaryDirectory(prefix='scrna-paper-') as temp:
            source, result = Path(temp) / 'paper.pdf', Path(temp) / 'text.json'
            source.write_bytes(data)
            with open(os.devnull, 'wb') as quiet:
                process = subprocess.Popen([sys.executable, '-m', 'pbmc_platform.app.paper_text', str(source), str(result)],
                                           stdout=quiet, stderr=quiet)
                try:
                    start = time.monotonic()
                    while process.poll() is None:
                        if time.monotonic() - start > 20:
                            raise ValueError('PDF parsing timed out; use a shorter methods PDF')
                        try:
                            if psutil.Process(process.pid).memory_info().rss > 384 * 1024 * 1024:
                                raise ValueError('PDF needs too much memory; use a simpler text-based PDF')
                        except psutil.NoSuchProcess:
                            break
                        time.sleep(.05)
                    if not result.exists() or result.stat().st_size > 1024 * 1024:
                        raise ValueError('Could not read this PDF')
                    output = json.loads(result.read_text(encoding='utf-8'))
                    if process.returncode or 'error' in output:
                        raise ValueError(output.get('error', 'Could not read this PDF'))
                    return output['pages']
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
    finally:
        _PARSER.release()


def save_paper(service, owner, run_id, filename, data):
    run = service.owned('runs', run_id, owner)
    read_package(service, run)
    pages = parse_pdf(data)
    identity = uuid.uuid4().hex
    prefix = f'runs/{run_id}/papers/{identity}'
    filename = re.sub(r'[^A-Za-z0-9_. -]', '_', Path(filename).name)[:120] or 'research-paper.pdf'
    record = {'id': identity, 'run_id': run_id, 'owner': owner, 'filename': filename,
              'status': 'ready', 'page_count': len(pages), 'created_at': now(), 'expires_at': run['expires_at']}
    key, index, run_key = 'papers/' + identity, 'paper_indexes/' + run_id, 'runs/' + run_id
    def reserve(values):
        current = values[run_key]
        if not current or current['owner'] != owner or current['status'] != 'succeeded':
            raise ValueError('Run is no longer available')
        ids = (values[index] or {}).get('ids', [])
        if len(ids) >= 3:
            raise ValueError('Keep up to three papers per run; remove one first')
        return None, {index: {'ids': ids + [identity]}, key: {**record, 'status': 'saving'}}
    service.store.atomic([key, index, run_key], reserve)
    try:
        pdf = service.blobs.write(prefix + '/paper.pdf', data)
        text = service.blobs.write(prefix + '/text.json', encode({'pages': pages}))
        def complete(values):
            current = values[run_key]
            if not current or current['status'] != 'succeeded' or current['owner'] != owner:
                raise ValueError('Run is no longer available')
            return None, {key: {**record, 'pdf': pdf, 'text': text}}
        service.store.atomic([key, run_key], complete)
    except Exception:
        remove_paper(service, owner, run_id, identity, check_run=False)
        raise
    return public_paper(service.store.get(key))


def public_paper(record):
    return {k: record[k] for k in ('id', 'run_id', 'filename', 'page_count', 'created_at', 'expires_at')}


def paper_record(service, owner, run_id, paper_id):
    service.owned('runs', run_id, owner)
    paper = service.store.get('papers/' + valid_id(paper_id))
    if not paper or paper['owner'] != owner or paper['run_id'] != run_id or paper['status'] != 'ready' or paper['expires_at'] <= now():
        raise LookupError('Paper not found')
    return paper


def paper_pages(service, record):
    return json.loads(object_read(service, record['text']))['pages']


def remove_paper(service, owner, run_id, paper_id, check_run=True):
    if check_run:
        paper_record(service, owner, run_id, paper_id)
    key, index = 'papers/' + valid_id(paper_id), 'paper_indexes/' + valid_id(run_id)
    def deleting(values):
        paper = values[key]
        if not paper or paper['owner'] != owner or paper['run_id'] != run_id:
            raise LookupError('Paper not found')
        return None, {key: {**paper, 'status': 'deleting'}}
    service.store.atomic([key], deleting)
    service.blobs.delete_prefix(f'runs/{run_id}/papers/{paper_id}')
    def deleted(values):
        ids = [x for x in (values[index] or {}).get('ids', []) if x != paper_id]
        paper = values[key]
        return None, {index: {'ids': ids}, key: {'id': paper_id, 'owner': owner, 'run_id': run_id,
                    'expires_at': paper['expires_at'], 'status': 'deleted'}}
    service.store.atomic([key, index], deleted)
    return {'status': 'deleted'}


def normalized(text):
    return ' '.join(text.split())


def validate_citations(pages, rules):
    for rule in rules:
        page = next((p for p in pages if p['page'] == rule.page), None)
        if not page or normalized(rule.quote) not in normalized(page['text']):
            raise ValueError('Quote must match the extracted text on its cited PDF page')
        numbers = [float(n.replace(',', '')) for n in re.findall(r'\d+(?:,\d{3})*(?:\.\d+)?', rule.quote)]
        if rule.value not in numbers:
            raise ValueError('The threshold number must appear in its quoted passage')


def compare_reference(service, owner, run_id, paper_id, reference):
    record = service.owned('runs', run_id, owner)
    package = read_package(service, record)
    paper = paper_record(service, owner, run_id, paper_id)
    pages = paper_pages(service, paper)
    validate_citations(pages, reference.rules)
    marker = json.loads(object_read(service, record['publication']))
    plot = json.loads(object_read(service, marker['objects']['plots.json']))
    n = package['metrics']['cells_total']
    baseline = np.asarray(plot['passing'], dtype=bool)
    if any(len(plot[k]) != n for k in ('passing', 'total_counts', 'detected_genes', 'mitochondrial_pct')):
        raise ValueError('Published plot arrays differ from the verified sample size')
    counts = np.asarray(plot['total_counts'])
    genes = np.asarray(plot['detected_genes'])
    mito = np.asarray([np.nan if x is None else x for x in plot['mitochondrial_pct']])
    kept = counts > 0
    operators = {'>=': np.greater_equal, '>': np.greater, '<': np.less, '<=': np.less_equal}
    for rule in reference.rules:
        values = mito if rule.field == 'max_mito_pct' else genes
        kept &= operators[rule.operator](values, rule.value) & np.isfinite(values)
    fields = {r.field for r in reference.rules}
    warnings = ['Only the confirmed supported rules are applied. Other paper methods are not reproduced.',
                'Zero-count cells are excluded. This comparison does not establish biological validity or a universal QC standard.']
    if len(fields) < 3:
        warnings.append('Partial reference: one or more gene/mitochondrial thresholds were not specified and are not applied.')
    if reference.species != package['configuration']['species']:
        warnings.append('Paper species is different or unspecified; applicability is not established.')
    if reference.tissue.lower().strip() != package['configuration']['tissue'].lower().strip():
        warnings.append('Paper tissue differs or is unspecified; review biological applicability.')
    return {'run_id': run_id, 'paper_id': paper_id, 'paper_filename': paper['filename'], 'cells_total': n,
            'current_pass': int(baseline.sum()), 'reference_pass': int(kept.sum()),
            'both_pass': int((baseline & kept).sum()), 'only_current': int((baseline & ~kept).sum()),
            'only_reference': int((~baseline & kept).sum()), 'current_policy': package['configuration'],
            'confirmed_reference': reference.model_dump(), 'warnings': warnings}
