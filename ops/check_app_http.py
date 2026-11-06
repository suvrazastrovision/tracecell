"""Exercise the running loopback app over HTTP, including its background worker."""
import json
from pathlib import Path
import time
import uuid
import requests


def main():
    base='http://127.0.0.1:8767'
    client=requests.Session();client.trust_env=False
    response=client.get(base+'/',timeout=30);response.raise_for_status();assert 'TraceCell' in response.text
    headers={'Origin':base,'X-CSRF-Token':client.cookies['csrf']}
    def call(method,path,**kwargs):
        response=client.request(method,base+path,headers=headers,timeout=60,**kwargs)
        response.raise_for_status();return response.json()
    catalog=call('GET','/api/examples');spec=catalog['human_pbmc3k']
    upload=call('POST','/api/examples/human_pbmc3k',json={})
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        record=next(x for x in call('GET','/api/uploads') if x['id']==upload['id'])
        if record['status']=='ready':break
        if record['status']=='failed':raise RuntimeError(record.get('error'))
        call('GET','/api/runs');time.sleep(2)
    else:raise TimeoutError('Inspection did not complete')
    run=call('POST','/api/runs',json={'upload_id':upload['id'],'idempotency_key':'http-'+uuid.uuid4().hex,'configuration':spec['example_configuration']})
    deadline=time.monotonic()+240
    while time.monotonic()<deadline:
        record=next(x for x in call('GET','/api/runs') if x['id']==run['id'])
        if record['status']=='succeeded':break
        if record['status']=='failed':raise RuntimeError(record.get('error'))
        time.sleep(3)
    else:raise TimeoutError('Processing did not complete')
    package=call('GET',f'/api/runs/{run["id"]}/results');assert package['metrics']['cells_pass']==2638
    answer=call('GET',f'/api/runs/{run["id"]}/answer?topic=qc');assert answer['facts']['cells_fail']==62
    report_response=client.get(base+f'/api/runs/{run["id"]}/download/report.md',timeout=60);report_response.raise_for_status()
    assert '2638' in report_response.text
    report={'status':'success','scope':'Real local HTTP routes, background subprocess, DuckDB/dbt, validation and downloads; no screenshot claim.',
            'run_id':run['id'],'metrics':package['metrics'],'answer_json_pointer':answer['json_pointer'],'report_download_verified':True}
    Path('artifacts/app-http-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
