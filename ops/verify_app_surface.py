"""Read-only cloud surface checks; tokens remain in memory, never in reports."""
import json
import os
from pathlib import Path
import subprocess
import requests
from pbmc_platform.app.runtime import authorized_session


def main():
    api=authorized_session();project='pbmc-okf-portfolio';resource=f'projects/{project}/locations/europe-west1/services/scrna-app'
    response=api.get('https://run.googleapis.com/v2/'+resource,timeout=60);response.raise_for_status();service=response.json()
    assert service['terminalCondition']['state']=='CONDITION_SUCCEEDED'
    response=api.get('https://run.googleapis.com/v2/'+resource+':getIamPolicy',timeout=60);response.raise_for_status();policy=response.json()
    assert not any(m in ('allUsers','allAuthenticatedUsers') for binding in policy.get('bindings',[]) for m in binding.get('members',[]))
    container=service['template']['containers'][0]
    env={x['name']:x.get('value') for x in container['env']}
    origin=env['SCRNA_ORIGIN'];assert env['SCRNA_MODE']=='cloud'
    anonymous=requests.get(origin+'/',timeout=60);assert anonymous.status_code==403
    gcloud=Path(os.environ['LOCALAPPDATA'])/'Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd'
    token=subprocess.run([str(gcloud),'auth','print-identity-token','--quiet'],check=True,capture_output=True,text=True).stdout.strip()
    web=requests.Session();web.headers['Authorization']='Bearer '+token
    ready=web.get(origin+'/ready',timeout=60);ready.raise_for_status();assert ready.json()['mode']=='cloud'
    html=web.get(origin+'/',timeout=60);html.raise_for_status();assert 'TraceCell' in html.text
    assert web.get(origin+'/api/runs',timeout=60).status_code==401
    csrf=web.cookies.get('csrf')
    internal=web.post(origin+'/internal/dispatch',json={},timeout=60)
    assert internal.status_code==403
    report={'status':'success','scope':'Cloud IAM, startup, HTML and rejection boundaries; invited Firebase browser login remains pending.',
            'uri':service['uri'],'origin':origin,'ready_revision':service['latestReadyRevision'],
            'image':container['image'],'anonymous_http_status':anonymous.status_code,'google_token_without_app_session_http_status':401,
            'wrong_internal_identity_http_status':internal.status_code,'public_invoker':False,
            'invited_user_count':len(env['SCRNA_ALLOWED_EMAILS'].split(',')),
            'firebase_public_config_present':bool(json.loads(env.get('SCRNA_FIREBASE_CONFIG','{}')))}
    Path('artifacts/app-surface-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
