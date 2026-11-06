"""Resumable real-cloud checks; no public access or user impersonation."""
import argparse
import json
import os
from pathlib import Path
import time
import uuid

from pbmc_platform.app.contracts import Configuration, Submission, Upload
from pbmc_platform.app.runtime import dispatch_cloud, authorized_session
from pbmc_platform.app.worker import read_package
from pbmc_platform.app.web import make_service


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,default=Path('artifacts/app-cloud-acceptance.json'))
    parser.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args()
    os.environ.update(SCRNA_MODE='cloud',GOOGLE_CLOUD_PROJECT='pbmc-okf-portfolio',
                      SCRNA_BUCKET='pbmc-okf-portfolio-scrna-app',SCRNA_WORKFLOW='scrna-app-pipeline')
    service=make_service()
    catalog=json.loads(Path('examples/app-datasets.json').read_text(encoding='utf-8'))
    report=json.loads(args.report.read_text(encoding='utf-8')) if args.report.exists() else {'owner':'cloud-check-'+uuid.uuid4().hex,'status':'running','examples':{}}
    def save():
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    def wait(kind,identity,success):
        deadline=time.monotonic()+1800
        while time.monotonic()<deadline:
            record=service.store.get(kind+'/'+identity)
            if record['status']==success:return record
            if record['status']=='failed':raise RuntimeError('Operation failed; inspect report and retained guard: '+record.get('error',''))
            dispatch_cloud(service)
            time.sleep(5)
        raise TimeoutError('Operation still pending; rerun with the same report to inspect/resume, do not create a duplicate')
    save()
    for name,spec in catalog.items():
        item=report['examples'].setdefault(name,{})
        if 'upload_id' not in item:
            path=Path('data/app-examples')/spec['prepared_filename']
            upload=service.upload(report['owner'],Upload(filename=path.name,size=path.stat().st_size,public_data=True))
            item['upload_id']=upload['id'];save()
            service.blobs.write(upload['key'],path.read_bytes())
            service.finalize(report['owner'],upload['id'])
        upload=wait('uploads',item['upload_id'],'ready')
        if item.get('run_id'):
            previous=service.store.get('runs/'+item['run_id'])
            if previous['status']=='failed' and args.retry_failed:
                from pbmc_platform.app.runtime import recover
                recover(service,previous['id'],previous['execution'])
                item.setdefault('failure_history',[]).append({'run_id':previous['id'],'error':previous.get('error'),'execution':previous['execution']})
                item['attempt']=item.get('attempt',1)+1
                del item['run_id'];save()
        if 'run_id' not in item:
            run=service.submit(report['owner'],Submission(upload_id=upload['id'],idempotency_key='cloud-example-'+name+'-'+str(item.get('attempt',1)),
                                                        configuration=Configuration(**spec['example_configuration'])))
            item['run_id']=run['id'];save()
        run=wait('runs',item['run_id'],'succeeded')
        package=read_package(service,run)
        expected={'human_pbmc3k':(2700,2638,62),'mouse_brain_1k':(1301,1117,184)}[name]
        assert tuple(package['metrics'][k] for k in ('cells_total','cells_pass','cells_fail'))==expected
        assert package['dbt']['models']==6 and package['dbt']['tests']==30
        item.update(metrics=package['metrics'],publication=run['publication'],workflow_execution=run['execution'],status='verified')
        save()
    dispatch_cloud(service)  # Reconcile final completed dispatch without starting another run.
    report.update(status='success',scope='Real GCP jobs, Firestore, GCS, BigQuery, dbt and validation; browser login not covered.')
    save();print(json.dumps(report,indent=2))


if __name__=='__main__':main()
