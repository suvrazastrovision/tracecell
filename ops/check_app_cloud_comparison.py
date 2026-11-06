"""One new policy run admitted through the real Scheduler/API identity."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from pbmc_platform.app.contracts import Configuration, Submission
from pbmc_platform.app.web import make_service
from pbmc_platform.app.runtime import dispatch_cloud


def main():
    os.environ.update(SCRNA_MODE='cloud',GOOGLE_CLOUD_PROJECT='pbmc-okf-portfolio',
                      SCRNA_BUCKET='pbmc-okf-portfolio-scrna-app',SCRNA_WORKFLOW='scrna-app-pipeline')
    path=Path('artifacts/app-cloud-acceptance.json');report=json.loads(path.read_text(encoding='utf-8'))
    assert report['status']=='success'
    service=make_service();baseline=report['examples']['human_pbmc3k']
    original=service.owned('runs',baseline['run_id'],report['owner'])
    config=Configuration(**{**original['configuration'],'max_mito_pct':10})
    run=service.submit(report['owner'],Submission(upload_id=original['upload_id'],idempotency_key='cloud-policy-comparison',configuration=config))
    report['comparison_run_id']=run['id'];path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    if run['status']=='queued':
        subprocess.run([sys.executable,'-m','ops.check_app_scheduler'],check=True)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            run=service.store.get('runs/'+run['id'])
            if run['status']!='queued':break
            time.sleep(3)
        else:raise TimeoutError('Scheduler did not admit the queued run; no operator fallback start was attempted')
    deadline=time.monotonic()+1800
    while time.monotonic()<deadline:
        run=service.store.get('runs/'+run['id'])
        if run['status']=='succeeded':break
        if run['status']=='failed':raise RuntimeError(run.get('error','Cloud comparison failed'))
        dispatch_cloud(service)
        time.sleep(5)
    else:raise TimeoutError('Comparison still pending; rerun this idempotent checker')
    result=service.compare(report['owner'],original['id'],run['id'])
    assert result['left_pass']==2638 and result['right_pass']==2689 and result['only_right']==51 and result['only_left']==0
    report['cloud_policy_comparison']=result
    report['comparison_workflow_execution']=run['execution']
    report['comparison_publication']=run['publication']
    dispatch_cloud(service)
    path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps({'status':'success','comparison':result},indent=2))


if __name__=='__main__':main()
