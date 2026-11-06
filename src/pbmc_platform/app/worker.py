"""Isolated stage workers and independently checked application publications."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile

import numpy as np
import pandas as pd
import yaml

from .adapter import inspect, qc_frames, selected
from .blobs import encode, sha
from .contracts import Configuration, REASONS, digest, now
from ..ingestion import sha256_file
from ..load_warehouse import prepare_load, load_bigquery, load_local, compare_rows
from ..run_cloud_dbt import summarize_results
from ..run_qc import OUTPUTS

MODELS = ('stg_cells', 'stg_genes', 'stg_dataset_summary', 'mart_dataset_qc', 'mart_qc_exclusions', 'mart_cohort_cells')


def object_read(service, reference):
    data = service.blobs.read(reference['key'], reference['generation'])
    if len(data) != reference['size'] or sha(data) != reference['sha256']:
        raise ValueError('Pinned object checksum or length differs')
    return data


def input_file(service, record, root):
    data = service.blobs.read(record['input']['key'], record['input']['generation'])
    if len(data) != record['input']['size']:
        raise ValueError('Input size changed')
    if record['input'].get('sha256') and sha(data) != record['input']['sha256']:
        raise ValueError('Input checksum changed')
    path = root / 'input.h5ad'
    path.write_bytes(data)
    return path


def code_identity():
    base = Path(__file__).resolve().parents[1]
    return {str(p.relative_to(base)).replace('\\', '/'): sha256_file(p)
            for p in sorted([*Path(__file__).parent.glob('*.py'), base / 'qc.py'])}


def build_release(service, record, root):
    config = Configuration(**record['configuration'])
    service.blobs.write(f'runs/{record["id"]}/configuration.json', encode(record['configuration']))
    path = input_file(service, record, root)
    matrix, cells, genes, summary = qc_frames(path, config, record['id'])
    identity = {'application_contract': '2.0.0', 'run_id': record['id'], 'input': record['input'],
                'configuration': config.model_dump(), 'code_sha256': code_identity()}
    # Include actual package versions as well as the image digest configured in deployment.
    from importlib.metadata import version
    identity['runtime'] = {name: version(name) for name in ('anndata', 'numpy', 'pandas', 'scipy', 'pyarrow', 'dbt-core')}
    release = digest(identity)
    directory = root / 'processed' / 'releases' / release
    directory.mkdir(parents=True)
    for name, frame in [('cells', cells), ('genes', genes), ('dataset_summary', summary)]:
        frame.to_parquet(directory / (name + '.parquet'), index=False)
    matrix.obs = cells.set_index('cell_id')
    matrix.var = genes.set_index('gene_id')
    matrix.write_h5ad(directory / 'counts.h5ad', compression='gzip', convert_strings_to_categoricals=False)
    metadata = {'release_id': release, 'identity': identity, 'summary': summary.iloc[0].to_dict(),
                'outputs': {name: {'sha256': sha256_file(directory / name)} for name in OUTPUTS}}
    (directory / 'release.json').write_bytes(encode(metadata))
    refs = {name: service.blobs.write(f'runs/{record["id"]}/release/{name}', (directory / name).read_bytes())
            for name in (*OUTPUTS, 'release.json')}
    return {'release_id': release, 'release': refs}


def restore_release(service, record, root):
    directory = root / 'processed' / 'releases' / record['release_id']
    directory.mkdir(parents=True)
    for name, ref in record['release'].items():
        (directory / name).write_bytes(object_read(service, ref))
    return prepare_load(root / 'processed', record['release_id']), directory


def database(service, root=None):
    if hasattr(service.blobs, 'root'):
        return service.blobs.root.parent / 'analytics.duckdb'
    return None


def load_stage(service, record, root):
    plan, _ = restore_release(service, record, root)
    local = database(service)
    if local:
        result = load_local(plan, local, 'scrna_app_raw')
    else:
        result = load_bigquery(plan, os.environ['GOOGLE_CLOUD_PROJECT'], 'scrna_app_raw', create_dataset=False)
    return {'warehouse': result}


def dbt_project(root, release, local):
    source = Path(__file__).resolve().parents[3] / 'warehouse' / 'dbt'
    destination = root / 'dbt'
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('target*', 'logs', 'dbt_packages', '.user.yml'))
    # Inherited images can contain read-only templates. Change only this private copy.
    destination.chmod(0o700)
    for item in destination.rglob('*'):
        item.chmod(0o700 if item.is_dir() else 0o600)
    # Keep the six tested SQL models; isolate their aliases in pre-provisioned app datasets.
    (destination / 'macros' / 'generate_alias_name.sql').write_text(
        "{% macro generate_alias_name(custom_alias_name=none, node=none) -%}\n"
        "{{ node.name ~ '_r_' ~ env_var('PBMC_RELEASE_ID') }}\n{%- endmacro %}\n", encoding='utf-8')
    profile = {'pbmc_analytics': {'target': 'local' if local else 'bigquery', 'outputs': {
        'local': {'type': 'duckdb', 'path': str(local or root / 'unused.duckdb'), 'schema': 'scrna_app_analytics', 'threads': 2},
        'bigquery': {'type': 'bigquery', 'method': 'oauth', 'project': os.getenv('GOOGLE_CLOUD_PROJECT', 'unconfigured-project'),
                     'dataset': 'scrna_app_analytics', 'location': 'europe-west1', 'threads': 2,
                     'maximum_bytes_billed': 100000000, 'job_execution_timeout_seconds': 300, 'job_retries': 1}}}}
    (destination / 'profiles.yml').write_text(yaml.safe_dump(profile), encoding='utf-8')
    return destination


def dbt_stage(service, record, root):
    local = database(service)
    release = record['release_id']
    project = dbt_project(root, release, local)
    env = {**os.environ, 'PBMC_RELEASE_ID': release, 'PBMC_RAW_DATASET': 'scrna_app_raw',
           'DBT_SEND_ANONYMOUS_USAGE_STATS': 'false'}
    command = [sys.executable, '-m', 'dbt.cli.main', 'build', '--project-dir', str(project),
               '--profiles-dir', str(project), '--target', 'local' if local else 'bigquery', '--no-partial-parse']
    # dbt's module is not a CLI entrypoint in every supported installation; use its console script.
    executable = Path(sysconfig.get_path('scripts')) / ('dbt.exe' if os.name == 'nt' else 'dbt')
    command = [str(executable), *command[3:]]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1100)
    if result.returncode:
        # Keep diagnostic logs private; callers receive a stage-level error.
        service.blobs.write(f'runs/{record["id"]}/dbt/failure.log', (result.stdout + result.stderr)[-100000:].encode())
        raise ValueError('SQL build or data tests failed; inspect the private dbt log')
    target = project / 'target'
    manifest = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
    invocation = manifest['metadata']['invocation_id']
    summary = summarize_results(target, record['id'], release, 'scrna_app_analytics')
    if summary['models'] != 6 or summary['tests'] != 30:
        raise ValueError('Unexpected app dbt model/test set')
    refs = {name: service.blobs.write(f'runs/{record["id"]}/dbt/{name}', (target / name).read_bytes())
            for name in ('manifest.json', 'run_results.json')}
    return {'dbt': summary, 'dbt_artifacts': refs}


def analytics_rows(service, record, model):
    if model not in MODELS:
        raise ValueError('Unknown model')
    table = model + '_r_' + record['release_id']
    local = database(service)
    if local:
        import duckdb
        with duckdb.connect(str(local), read_only=True) as db:
            return db.execute(f'SELECT * FROM scrna_app_analytics."{table}"').fetchdf()
    from google.cloud import bigquery
    client = bigquery.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'])
    table_id = f'{client.project}.scrna_app_analytics.{table}'
    definition = client.get_table(table_id)
    if definition.table_type == 'VIEW':
        sql = f'SELECT * FROM `{table_id}`'
        cap = 100000000
        dry = client.query(sql, location='europe-west1', job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, maximum_bytes_billed=cap))
        if dry.total_bytes_processed > cap:
            raise ValueError('Validation view query exceeds the 100 MB scan cap')
        rows = client.query(sql, location='europe-west1', job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=cap)).result(timeout=300)
    else:
        rows = client.list_rows(table_id)
    return pd.DataFrame([dict(row) for row in rows], columns=[field.name for field in definition.schema])


def raw_readback(service, record, plan):
    local = database(service)
    if local:
        import duckdb
        with duckdb.connect(str(local), read_only=True) as db:
            for name, spec in plan['tables'].items():
                actual = db.execute(f'SELECT * FROM scrna_app_raw."{spec["table_name"]}"').fetchdf()
                compare_rows(name, actual, pd.read_parquet(spec['file']))
    else:
        from google.cloud import bigquery
        client = bigquery.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'])
        for name, spec in plan['tables'].items():
            actual = pd.DataFrame([dict(row) for row in client.list_rows(f'{client.project}.scrna_app_raw.{spec["table_name"]}')])
            compare_rows(name, actual, pd.read_parquet(spec['file']))


def knowledge_stage(service, record, root):
    config = Configuration(**record['configuration'])
    plan, directory = restore_release(service, record, root)
    raw_readback(service, record, plan)
    source = selected(input_file(service, record, root), config)
    # Independently calculate each cell's metrics/flags, without the producer's QC function.
    counts = np.asarray(source.X.sum(axis=1)).ravel()
    genes = np.diff(source.X.indptr)
    mito = np.asarray(source.X[:, source.var.app_mito.to_numpy()].sum(axis=1)).ravel()
    pct = np.divide(mito * 100, counts, out=np.full(source.n_obs, np.nan), where=counts > 0)
    flags = {'zero_counts': counts == 0, 'low_genes': genes < config.min_genes,
             'high_genes': genes >= config.max_genes, 'high_mito': pct >= config.max_mito_pct}
    passed = ~np.logical_or.reduce(list(flags.values()))
    cells = pd.read_parquet(directory / 'cells.parquet')
    expected_ids = ['sample_' + record['id'] + ':1:' + str(v) for v in source.obs_names]
    if cells.cell_id.tolist() != expected_ids:
        raise ValueError('Source cell identities differ')
    for column, values in {'total_counts': counts, 'n_genes_by_counts': genes,
                           'mitochondrial_counts': mito, 'pct_counts_mito': pct, 'qc_pass': passed, **flags}.items():
        if not np.allclose(cells[column].to_numpy(), values, rtol=0, atol=1e-12, equal_nan=True):
            raise ValueError('Independent source calculation differs: ' + column)
    expected_reasons = [';'.join(k for k in REASONS if flags[k][i]) for i in range(source.n_obs)]
    if cells.exclusion_reasons.tolist() != expected_reasons:
        raise ValueError('Independent exclusion reasons differ')
    source_genes = pd.read_parquet(directory / 'genes.parquet')
    if source_genes.gene_id.tolist() != source.var.gene_ids.tolist() or source_genes.source_feature_name.tolist() != source.var.source_symbol.tolist() or source_genes.is_mitochondrial.tolist() != source.var.app_mito.tolist():
        raise ValueError('Source gene metadata differs')
    metrics = {'cells_total': source.n_obs, 'cells_pass': int(passed.sum()), 'cells_fail': int((~passed).sum()),
               'features_total': source.n_vars, 'total_counts': int(counts.sum()),
               'mitochondrial_features': int(source.var.app_mito.sum()), 'qc_pass_fraction': float(passed.mean())}
    exclusions = {k: int(v.sum()) for k, v in flags.items()}
    for model, name in [('stg_cells', 'cells'), ('stg_genes', 'genes'), ('stg_dataset_summary', 'dataset_summary')]:
        frame = analytics_rows(service, record, model)
        if set(frame.release_id) != {record['release_id']}:
            raise ValueError('Staging release differs')
        compare_rows(name, frame.drop(columns='release_id'), pd.read_parquet(directory / (name + '.parquet')))
    mart = analytics_rows(service, record, 'mart_dataset_qc')
    if len(mart) != 1 or any(not np.isclose(float(mart.iloc[0][k]), value, rtol=0, atol=1e-12) for k, value in metrics.items()):
        raise ValueError('Analytics summary differs from independent source calculations')
    reasons = analytics_rows(service, record, 'mart_qc_exclusions')
    if len(reasons) != 4 or dict(zip(reasons.exclusion_reason, reasons.excluded_cells)) != exclusions:
        raise ValueError('Analytics exclusions differ')
    cohort = analytics_rows(service, record, 'mart_cohort_cells')
    expected_cohort = cells.loc[passed].copy()
    compare_rows('cells', cohort.drop(columns='release_id'), expected_cohort)
    for name, reference in record['dbt_artifacts'].items():
        (root / name).write_bytes(object_read(service, reference))
    dbt = summarize_results(root, record['id'], record['release_id'], 'scrna_app_analytics')
    if dbt != record['dbt']:
        raise ValueError('dbt receipt differs from pinned artifacts')
    package = {'format_name': 'scrna-app-knowledge', 'schema_version': '2.0.0', 'status': 'verified',
               'run_id': record['id'], 'release_id': record['release_id'], 'input': record['input'],
               'configuration': record['configuration'], 'metrics': metrics, 'exclusions': exclusions,
               'passing_barcodes': source.obs_names[passed].tolist(), 'dbt': dbt,
               'evidence': {'release': record['release'], 'dbt': record['dbt_artifacts']},
               'limitations': ['User-declared single research sample; species and tissue are not inferred.',
                               'QC thresholds are technical choices, not a clinical or biological conclusion.',
                               'No cell-type inference, disease comparison or cross-species integration.',
                               'Exclusion reasons may overlap.']}
    from jsonschema import Draft202012Validator
    schema = Path(__file__).resolve().parents[3] / 'schemas' / 'scrna-app-knowledge-v2.schema.json'
    Draft202012Validator(json.loads(schema.read_text(encoding='utf-8'))).validate(package)
    validation = {'schema_version': '2.0.0', 'status': 'success', 'run_id': record['id'],
                  'checks': ['input_generation_checksum', 'source_recomputation', 'raw_readback', 'staging_readback',
                             'analytics_reconciliation', 'exact_cohort', 'successful_pinned_dbt_artifacts'],
                  'validator_sha256': sha256_file(Path(__file__)), 'schema_sha256': sha256_file(schema),
                  'knowledge_sha256': sha(encode(package))}
    report = '# Verified single-cell QC report\n\n'
    report += f'Species: {config.species}. Tissue: {config.tissue}.\n\n'
    report += '\n'.join(f'- {k}: {v}' for k, v in metrics.items())
    report += '\n\n## Exclusions\n\n' + '\n'.join(f'- {k}: {v}' for k, v in exclusions.items())
    report += '\n\n## Limitations\n\n' + '\n'.join('- ' + x for x in package['limitations']) + '\n'
    plot = {'total_counts': counts.astype(int).tolist(), 'detected_genes': genes.astype(int).tolist(),
            'mitochondrial_pct': [None if np.isnan(v) else float(v) for v in pct], 'passing': passed.tolist()}
    output = {'knowledge.json': encode(package), 'validation.json': encode(validation), 'report.md': report.encode(),
              'plots.json': encode(plot), 'passing_cells.csv': expected_cohort.to_csv(index=False).encode()}
    objects = {name: service.blobs.write(f'runs/{record["id"]}/published/{name}', data) for name, data in output.items()}
    marker = {'schema_version': '2.0.0', 'run_id': record['id'], 'release_id': record['release_id'],
              'package_id': digest(objects), 'objects': objects}
    publication = service.blobs.write(f'runs/{record["id"]}/published/publication.json', encode(marker))
    return {'publication': publication, 'metrics': metrics}


def read_package(service, record):
    if record['status'] != 'succeeded':
        raise ValueError('Run is not complete')
    marker = json.loads(object_read(service, record['publication']))
    if marker['run_id'] != record['id'] or marker['release_id'] != record['release_id'] or marker['package_id'] != digest(marker['objects']):
        raise ValueError('Publication identity differs')
    for name, reference in marker['objects'].items():
        if reference['key'] != f'runs/{record["id"]}/published/{name}':
            raise ValueError('Publication reference is outside selected run')
    files = {name: object_read(service, ref) for name, ref in marker['objects'].items()}
    package, validation = json.loads(files['knowledge.json']), json.loads(files['validation.json'])
    if validation['status'] != 'success' or validation['knowledge_sha256'] != sha(files['knowledge.json']) or package['run_id'] != record['id']:
        raise ValueError('Validation evidence differs')
    return package


def execute_stage(service, kind, identity, execution, stage):
    from .service import valid_id
    valid_id(identity)
    record = service.store.get(kind + '/' + identity)
    if stage in ('inspect', 'qc'):
        if not service.claim(kind, identity, execution):
            raise ValueError('Run already admitted, duplicate, expired or processing slot occupied')
        record = service.store.get(kind + '/' + identity)
    elif not record or record.get('execution') != execution or record['status'] != 'running':
        raise ValueError('Run is not owned by this workflow')
    required = {'load': 'qc', 'dbt': 'load', 'knowledge': 'dbt'}
    if stage in required and record.get('completed_stage') != required[stage]:
        raise ValueError('Upstream stage is incomplete')
    try:
        with tempfile.TemporaryDirectory(prefix='scrna-app-') as temp:
            root = Path(temp)
            if stage == 'inspect':
                path = input_file(service, record, root)
                local = hasattr(service.blobs, 'root')
                result = {'inspection': inspect(path), 'status': 'ready' if local else 'inspected',
                          'input': {**record['input'], 'sha256': sha256_file(path)}}
                return service.finish(kind, identity, execution, result, release=local)
            service.finish(kind, identity, execution, {'stage': stage}, release=False)
            function = {'qc': build_release, 'load': load_stage, 'dbt': dbt_stage, 'knowledge': knowledge_stage}[stage]
            updates = function(service, record, root)
            final = stage == 'knowledge'
            local = hasattr(service.blobs, 'root')
            updates.update(completed_stage=stage, status=('succeeded' if local else 'published') if final else 'running')
            return service.finish(kind, identity, execution, updates, release=final and local)
    except Exception as error:
        # Fail closed. Inspecting/recovering terminal workflow and Cloud Run tasks is a separate operation.
        service.finish(kind, identity, execution, {'status': 'failed', 'stage': stage,
                       'error': str(error)[:500]}, release=False)
        raise


def process_local(service, kind, identity):
    execution = 'local-' + identity
    stages = ['inspect'] if kind == 'uploads' else ['qc', 'load', 'dbt', 'knowledge']
    for stage in stages:
        execute_stage(service, kind, identity, execution, stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['uploads', 'runs'], required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--execution', required=True)
    parser.add_argument('--stage', choices=['inspect', 'qc', 'load', 'dbt', 'knowledge'], required=True)
    args = parser.parse_args()
    from .web import make_service
    execute_stage(make_service(), args.kind, args.id, args.execution, args.stage)


if __name__ == '__main__':
    main()
