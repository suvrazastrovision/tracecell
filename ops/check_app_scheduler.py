"""Verify one real app Scheduler dispatch and always restore its paused state."""
import json
import argparse
from pathlib import Path
import time
from datetime import datetime, timezone
from pbmc_platform.app.runtime import authorized_session


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--job',choices=['dispatch','cleanup'],default='dispatch');parser.add_argument('--verify-since');args=parser.parse_args()
    job_name='scrna-app-'+args.job
    api=authorized_session();url='https://cloudscheduler.googleapis.com/v1/projects/pbmc-okf-portfolio/locations/europe-west1/jobs/'+job_name
    response=api.get(url,timeout=30);response.raise_for_status();before=response.json();assert before['state']=='PAUSED'
    started=args.verify_since or datetime.now(timezone.utc).isoformat()
    report={'status':'running','job':job_name,'original_state':'PAUSED','started_at':started}
    try:
        if not args.verify_since:
            response=api.post(url+':resume',json={},timeout=30);response.raise_for_status()
            response=api.post(url+':run',json={},timeout=30);response.raise_for_status()
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            response=api.get(url,timeout=30);response.raise_for_status();job=response.json()
            filter_text='resource.type="cloud_scheduler_job" AND resource.labels.job_id="'+job_name+'" AND timestamp>="'+started+'"'
            entries=[];token=None
            while True:
                logs=api.post('https://logging.googleapis.com/v2/entries:list',json={'resourceNames':['projects/pbmc-okf-portfolio'],
                              'filter':filter_text,'orderBy':'timestamp desc','pageSize':50,**({'pageToken':token} if token else {})},timeout=30)
                logs.raise_for_status();body=logs.json();entries.extend(body.get('entries',[]));token=body.get('nextPageToken')
                if not token:break
            finished=[x for x in entries if x.get('jsonPayload',{}).get('@type','').endswith('AttemptFinished')]
            if finished:
                entry=finished[0];payload=entry['jsonPayload']
                if entry.get('httpRequest',{}).get('status')!=200 or payload.get('status') not in (None,'OK'):
                    raise RuntimeError('Scheduler delivery failed: '+str(payload.get('status')))
                report.update(status='success',last_attempt=job.get('lastAttemptTime'),target=job['httpTarget']['uri'],
                              finished_log_timestamp=entry['timestamp'],finished_http_status=entry['httpRequest']['status'],status_field_omitted='status' not in payload,debug_info=payload.get('debugInfo'))
                break
            time.sleep(3)
        else:raise TimeoutError('Scheduler attempt not observed')
    except Exception as error:
        report.update(status='failed',error=str(error));raise
    finally:
        if not args.verify_since:
            response=api.post(url+':pause',json={},timeout=30);response.raise_for_status()
        response=api.get(url,timeout=30);response.raise_for_status();assert response.json()['state']=='PAUSED'
        report['restored_paused']=True
        Path('artifacts/app-'+args.job+'-scheduler-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
