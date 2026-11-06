"""Actual API-account cleanup against disposable app-only table/object fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from pbmc_platform.app.contracts import Upload, digest
from pbmc_platform.app.web import make_service
from pbmc_platform.app.worker import read_package


def main():
    os.environ.update(SCRNA_MODE='cloud',GOOGLE_CLOUD_PROJECT='pbmc-okf-portfolio',
                      SCRNA_BUCKET='pbmc-okf-portfolio-scrna-app',SCRNA_WORKFLOW='scrna-app-pipeline')
    service=make_service();client=bigquery.Client(project='pbmc-okf-portfolio')
    path=Path('artifacts/app-cleanup-verification.json')
    report=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status':'running','owner':'retention-check-'+uuid.uuid4().hex}
    def save():path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    if 'upload_id' not in report:
        upload=service.upload(report['owner'],Upload(filename='deletion_fixture.h5ad',size=7,public_data=True))
        a,b=uuid.uuid4().hex,uuid.uuid4().hex;release=digest({'fixture':b})
        report.update(upload_id=upload['id'],already_deleted_run=a,partial_run=b,release_id=release);save()
        service.blobs.write(upload['key'],b'fixture')
        service.blobs.write(f'runs/{b}/fixture.txt',b'fixture')
        expiry='2000-01-01T00:00:00+00:00'
        service.store.put('runs/'+a,{'id':a,'owner':upload['owner'],'upload_id':upload['id'],'expires_at':expiry,'status':'deleted'})
        service.store.put('runs/'+b,{'id':b,'owner':upload['owner'],'upload_id':upload['id'],'expires_at':expiry,'status':'deleting','release_id':release})
        source_id=f'{client.project}.scrna_app_raw.cells_r_{release}'
        view_id=f'{client.project}.scrna_app_analytics.stg_cells_r_{release}'
        client.create_table(bigquery.Table(source_id,schema=[bigquery.SchemaField('fixture','INTEGER')]))
        view=bigquery.Table(view_id);view.view_query='SELECT 1 AS fixture';client.create_table(view)
        report['created_tables']=[source_id,view_id];save()
        # This simulates an interrupted prior deletion; no data processing is claimed.
        service.store.put('uploads/'+upload['id'],{**upload,'run_ids':[a,b],'expires_at':expiry,'status':'deleting'})
    subprocess.run([sys.executable,'-m','ops.check_app_scheduler','--job','cleanup'],check=True)
    assert service.store.get('uploads/'+report['upload_id'])['status']=='deleted'
    assert all(service.store.get('runs/'+report[k])['status']=='deleted' for k in ('already_deleted_run','partial_run'))
    for table in report['created_tables']:
        try:client.get_table(table)
        except NotFound:pass
        else:raise AssertionError('Fixture table/view remains: '+table)
    for prefix in ('uploads/'+report['upload_id'],'runs/'+report['partial_run']):
        assert list(service.blobs.client.list_blobs(service.blobs.bucket,prefix=prefix+'/'))==[]
    baseline=json.loads(Path('artifacts/app-cloud-acceptance.json').read_text(encoding='utf-8'))
    for name,item in baseline['examples'].items():
        record=service.store.get('runs/'+item['run_id']);assert record['status']=='succeeded'
        read_package(service,record)
        client.get_table(f'{client.project}.scrna_app_raw.cells_r_{record["release_id"]}')
        client.get_table(f'{client.project}.scrna_app_analytics.mart_dataset_qc_r_{record["release_id"]}')
    report.update(status='success',scope='Real Scheduler ? Cloud Run API cleanup identity; expired disposable fixtures only.',
                  deleted_object_prefixes_verified=True,deleted_table_and_view_verified=True,partial_retry_verified=True,
                  human_mouse_baselines_unchanged=True)
    save();print(json.dumps(report,indent=2))


if __name__=='__main__':main()
