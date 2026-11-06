"""Admission, cloud dispatch, explicit recovery and bounded app-only cleanup."""
import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

from .contracts import now, digest
from .service import valid_id

JOBS = ('inspect', 'qc', 'load', 'dbt', 'knowledge')


def queued(service):
    items = [('uploads', x) for x in service.store.scan('uploads') if x['status'] == 'queued_inspection']
    items += [('runs', x) for x in service.store.scan('runs') if x['status'] == 'queued']
    return sorted([(kind, x) for kind, x in items if x['expires_at'] > now()], key=lambda item: item[1]['created_at'])


def dispatch_local(service):
    from .worker import process_local
    if (service.store.get('control/admission') or {}).get('paused'):
        return
    while not service.store.get('control/slot'):
        choices = queued(service)
        if not choices:
            return
        kind, record = choices[0]
        try:
            process_local(service, kind, record['id'])
        except Exception:
            import logging
            logging.exception('Application operation failed; processing slot retained')
            return


def authorized_session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
    return AuthorizedSession(credentials)


def dispatch_cloud(service):
    slot = service.store.get('control/slot')
    if slot:
        record = service.store.get(slot['kind'] + '/' + slot['owner']) or {}
        project = os.environ['GOOGLE_CLOUD_PROJECT']
        workflow = os.environ['SCRNA_WORKFLOW']
        response = authorized_session().get(
            f'https://workflowexecutions.googleapis.com/v1/projects/{project}/locations/europe-west1/workflows/{workflow}/executions/{slot["execution"]}', timeout=30)
        response.raise_for_status()
        state = response.json()['state']
        if state == 'SUCCEEDED' and record.get('status') in ('published', 'inspected'):
            if record['status'] == 'published':
                from .worker import read_package
                read_package(service, {**record, 'status': 'succeeded'})
            terminal = 'succeeded' if slot['kind'] == 'runs' else 'ready'
            service.finish(slot['kind'], slot['owner'], slot['execution'], {'status': terminal}, release=True)
            return {'status': 'finalized', 'id': slot['owner']}
        if state in ('FAILED', 'CANCELLED'):
            service.finish(slot['kind'], slot['owner'], slot['execution'],
                           {'status': 'failed', 'error': record.get('error') or 'Owning workflow failed or was cancelled'}, release=False)
            record = service.store.get(slot['kind'] + '/' + slot['owner'])
        timestamp = record.get('updated_at', record.get('started_at', slot['acquired_at']))
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()
        if record.get('status') == 'failed' or elapsed > 2100:
            print(json.dumps({'severity': 'ERROR', 'message': 'app_operation_failed_or_stalled', 'id': slot['owner']}), flush=True)
        return {'status': 'occupied'}
    if (service.store.get('control/admission') or {}).get('paused'):
        return {'status': 'paused'}
    pending = service.store.get('control/dispatch')
    if pending:
        record = service.store.get(pending['kind'] + '/' + pending['id'])
        if record and record['status'] in ('ready', 'succeeded', 'cancelled', 'deleted'):
            service.store.atomic(['control/dispatch'], lambda values: (None, {'control/dispatch': None})
                                   if values['control/dispatch'] == pending else (None, {}))
        else:
            # API timeout may have accepted a start; do not submit another workflow.
            print(json.dumps({'severity': 'ERROR' if pending.get('status') == 'uncertain' or (datetime.now(timezone.utc) - datetime.fromisoformat(pending['created_at'])).total_seconds() > 120 else 'INFO',
                              'message': 'app_dispatch_pending', 'id': pending['id']}), flush=True)
            return {'status': 'pending_start_or_recovery', 'id': pending['id']}
    choices = queued(service)
    if not choices:
        return {'status': 'idle'}
    kind, record = choices[0]
    dispatch = {'id': record['id'], 'kind': kind, 'nonce': uuid.uuid4().hex, 'status': 'starting', 'created_at': now()}
    keys = ['control/dispatch', 'control/slot', kind + '/' + record['id']]
    def reserve(values):
        current = values[kind + '/' + record['id']]
        if values['control/dispatch'] or values['control/slot'] or current['status'] not in ('queued', 'queued_inspection'):
            return False, {}
        return True, {'control/dispatch': dispatch}
    if not service.store.atomic(keys, reserve):
        return {'status': 'contended'}
    project = os.environ['GOOGLE_CLOUD_PROJECT']
    workflow = os.environ['SCRNA_WORKFLOW']
    url = f'https://workflowexecutions.googleapis.com/v1/projects/{project}/locations/europe-west1/workflows/{workflow}/executions'
    try:
        response = authorized_session().post(url, json={'argument': json.dumps(dispatch), 'callLogLevel': 'LOG_ERRORS_ONLY'}, timeout=30)
        response.raise_for_status()
        dispatch.update(status='launched', workflow_execution=response.json()['name'])
    except Exception:
        dispatch['status'] = 'uncertain'
        print(json.dumps({'severity': 'ERROR', 'message': 'app_dispatch_uncertain', 'id': record['id']}), flush=True)
    service.store.put('control/dispatch', dispatch)
    return {'status': dispatch['status'], 'id': dispatch['id']}


def cloud_quiescent(service, slot):
    session = authorized_session()
    project = os.environ['GOOGLE_CLOUD_PROJECT']
    workflow = os.environ['SCRNA_WORKFLOW']
    url = f'https://workflowexecutions.googleapis.com/v1/projects/{project}/locations/europe-west1/workflows/{workflow}/executions/{slot["execution"]}'
    response = session.get(url, timeout=30)
    response.raise_for_status()
    if response.json()['state'] not in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
        raise ValueError('Workflow is not terminal')
    state = response.json()['state']
    for stage in JOBS:
        token = None
        while True:
            response = session.get(f'https://run.googleapis.com/v2/projects/{project}/locations/europe-west1/jobs/scrna-app-{stage}/executions',
                                   params={'pageSize': 100, **({'pageToken': token} if token else {})}, timeout=30)
            response.raise_for_status()
            body = response.json()
            if any(not item.get('completionTime') for item in body.get('executions', [])):
                raise ValueError('A Cloud Run app execution is still active')
            token = body.get('nextPageToken')
            if not token:
                break
    return state


def recover(service, identity, execution, acknowledge_local_stopped=False):
    slot = service.store.get('control/slot')
    valid_id(identity)
    if not slot:
        pending = service.store.get('control/dispatch')
        if hasattr(service.blobs, 'root') or not pending or pending['id'] != identity or not pending.get('workflow_execution', '').endswith('/' + execution):
            raise ValueError('No matching recorded dispatch execution; inspect uncertain starts before recovery')
        state = cloud_quiescent(service, {'execution': execution})
        if state not in ('FAILED', 'CANCELLED'):
            raise ValueError('Dispatch owner has not failed or cancelled')
        def clear(values):
            if values['control/slot'] or values['control/dispatch'] != pending:
                raise ValueError('Dispatch owner changed')
            return {'recovered_dispatch': identity}, {'control/dispatch': None}
        return service.store.atomic(['control/slot', 'control/dispatch'], clear)
    if slot['owner'] != identity or slot['execution'] != execution:
        raise ValueError('Inspected processing owner differs')
    record = service.store.get(slot['kind'] + '/' + identity)
    if hasattr(service.blobs, 'root'):
        if record['status'] != 'failed':
            raise ValueError('Only a failed operation may be recovered')
        if not acknowledge_local_stopped:
            raise ValueError('Stop local dispatch processes and explicitly acknowledge before recovery')
    else:
        state = cloud_quiescent(service, slot)
        if state not in ('FAILED', 'CANCELLED') and record['status'] != 'failed':
            raise ValueError('Owner has not failed or cancelled')
    key = slot['kind'] + '/' + identity
    keys = ['control/slot', 'control/dispatch', key]
    def change(values):
        if values['control/slot'] != slot:
            raise ValueError('Processing owner changed during recovery')
        return {'recovered': identity}, {'control/slot': None, 'control/dispatch': None,
                                       key: {**values[key], 'status': 'failed', 'error': values[key].get('error', 'Workflow failed or cancelled; recovered after terminal task checks')}}
    return service.store.atomic(keys, change)


def delete_tables(service, record):
    release = record.get('release_id')
    if not release:
        return
    import re
    if not re.fullmatch('[a-f0-9]{64}', release):
        raise ValueError('Invalid deletion table suffix')
    local = hasattr(service.blobs, 'root')
    if local:
        import duckdb
        from .worker import database
        with duckdb.connect(str(database(service))) as db:
            for schema, models in [('scrna_app_raw', ['cells', 'genes', 'dataset_summary']),
                                    ('scrna_app_analytics', ['stg_cells', 'stg_genes', 'stg_dataset_summary', 'mart_dataset_qc', 'mart_qc_exclusions', 'mart_cohort_cells'])]:
                for model in models:
                    name = f'{schema}."{model}_r_{release}"'
                    kind = db.execute('SELECT table_type FROM information_schema.tables WHERE table_schema=? AND table_name=?',
                                      [schema, model + '_r_' + release]).fetchone()
                    if kind:
                        db.execute(f'DROP {"VIEW" if kind[0] == "VIEW" else "TABLE"} {name}')
    else:
        from google.cloud import bigquery
        client = bigquery.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'])
        from .worker import MODELS
        for schema, models in [('scrna_app_raw', ['cells', 'genes', 'dataset_summary']), ('scrna_app_analytics', MODELS)]:
            for model in models:
                client.delete_table(f'{client.project}.{schema}.{model}_r_{release}', not_found_ok=True)


def cleanup(service):
    deleted = 0
    for upload in service.store.scan('uploads'):
        if upload['status'] == 'deleted' or (upload['status'] != 'deleting' and upload['expires_at'] > now()):
            continue
        related = [r for r in service.store.scan('runs') if r['upload_id'] == upload['id']]
        ids = {upload['id'], *[r['id'] for r in related]}
        slot = service.store.get('control/slot') or {}
        pending = service.store.get('control/dispatch') or {}
        if slot.get('owner') in ids or pending.get('id') in ids:
            continue
        # Marking all resources atomically prevents a queued record being admitted during cleanup.
        keys = ['control/slot', 'control/dispatch', 'uploads/' + upload['id'], *['runs/' + r['id'] for r in related]]
        def mark(values):
            if set(values['uploads/' + upload['id']].get('run_ids', [])) != {r['id'] for r in related}:
                return False, {}
            if (values['control/slot'] or {}).get('owner') in ids or (values['control/dispatch'] or {}).get('id') in ids:
                return False, {}
            return True, {k: {**v, 'status': 'deleting'} for k, v in values.items() if k.startswith(('uploads/', 'runs/')) and v}
        if not service.store.atomic(keys, mark):
            continue
        for record in related:
            delete_tables(service, record)
            service.blobs.delete_prefix('runs/' + valid_id(record['id']))
            for paper in service.store.scan('papers'):
                if paper['run_id'] == record['id'] and paper['status'] != 'deleted':
                    service.store.put('papers/' + valid_id(paper['id']),
                                      {'id': paper['id'], 'owner': paper['owner'], 'run_id': record['id'],
                                       'expires_at': paper['expires_at'], 'status': 'deleted'})
            service.store.put('paper_indexes/' + valid_id(record['id']), None)
            service.store.put('runs/' + record['id'], {'id': record['id'], 'owner': record['owner'],
                              'upload_id': record['upload_id'], 'expires_at': record['expires_at'], 'status': 'deleted'})
        service.blobs.delete_prefix('uploads/' + valid_id(upload['id']))
        key, account = 'uploads/' + upload['id'], 'accounts/' + digest({'owner': upload['owner']})
        def tombstone(values):
            if values[key]['status'] == 'deleted':
                return None, {}
            return None, {key: {'id': upload['id'], 'owner': upload['owner'], 'expires_at': upload['expires_at'], 'status': 'deleted'},
                          account: {'retained': max(0, (values[account] or {}).get('retained', 1) - 1)}}
        service.store.atomic([key, account], tombstone)
        deleted += 1
    return {'deleted_uploads': deleted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['dispatch-local', 'recover', 'cleanup', 'pause', 'resume'])
    parser.add_argument('--id')
    parser.add_argument('--execution')
    parser.add_argument('--acknowledge-local-processes-stopped', action='store_true')
    args = parser.parse_args()
    from .web import make_service
    service = make_service()
    if args.action == 'dispatch-local':
        dispatch_local(service)
    elif args.action == 'cleanup':
        print(json.dumps(cleanup(service)))
    elif args.action in ('pause', 'resume'):
        service.store.put('control/admission', {'paused': args.action == 'pause'})
    else:
        print(json.dumps(recover(service, args.id, args.execution, args.acknowledge_local_processes_stopped)))


if __name__ == '__main__':
    main()
