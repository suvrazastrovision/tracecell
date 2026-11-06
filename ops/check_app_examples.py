"""Real-data local acceptance, with observed memory/time; no production writes."""
import json
import os
from pathlib import Path
import threading
import time
import uuid

from pbmc_platform.app.blobs import LocalBlobs
from pbmc_platform.app.contracts import Configuration, Submission, Upload
from pbmc_platform.app.service import Service
from pbmc_platform.app.store import LocalStore
from pbmc_platform.app.worker import execute_stage, process_local, read_package


def main():
    import psutil
    catalog=json.loads(Path('examples/app-datasets.json').read_text(encoding='utf-8'))
    root=Path('artifacts/app-acceptance')/uuid.uuid4().hex;root.mkdir(parents=True)
    service=Service(LocalStore(root/'state.sqlite'),LocalBlobs(root/'objects'))
    started=time.monotonic();stop=threading.Event();observed={'peak_process_tree_rss_bytes':0}
    def monitor():
        process=psutil.Process()
        while not stop.wait(.2):
            total=0
            for p in [process,*process.children(recursive=True)]:
                try:total+=p.memory_info().rss
                except psutil.Error:pass
            observed['peak_process_tree_rss_bytes']=max(observed['peak_process_tree_rss_bytes'],total)
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    report={'status':'running','examples':{},'scope':'Local real-data processing, real DuckDB/dbt; not a cloud capacity benchmark.'}
    try:
        for name,spec in catalog.items():
            path=Path('data/app-examples')/spec['prepared_filename']
            upload=service.upload('acceptance',Upload(filename=path.name,size=path.stat().st_size,public_data=True))
            service.blobs.write(upload['key'],path.read_bytes());service.finalize('acceptance',upload['id'])
            execute_stage(service,'uploads',upload['id'],'inspect-'+upload['id'],'inspect')
            config=Configuration(**spec['example_configuration'])
            run=service.submit('acceptance',Submission(upload_id=upload['id'],idempotency_key='example-'+name,configuration=config))
            tick=time.monotonic();process_local(service,'runs',run['id'])
            run=service.owned('runs',run['id'],'acceptance');package=read_package(service,run)
            if name=='human_pbmc3k':
                assert package['metrics']['cells_total']==2700 and package['metrics']['cells_pass']==2638 and package['metrics']['cells_fail']==62
            else:assert package['metrics']['cells_total']==1301
            report['examples'][name]={'run_id':run['id'],'metrics':package['metrics'],'seconds':round(time.monotonic()-tick,2),
                                      'dbt':package['dbt'],'publication':run['publication']}
            if name=='human_pbmc3k':
                variant=config.model_copy(update={'max_mito_pct':10})
                second=service.submit('acceptance',Submission(upload_id=upload['id'],idempotency_key='comparison-example',configuration=variant))
                process_local(service,'runs',second['id'])
                report['policy_comparison']=service.compare('acceptance',run['id'],second['id'])
        report['status']='success'
    finally:
        stop.set();thread.join();report.update(observed,seconds=round(time.monotonic()-started,2))
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'report':str(root/'report.json'),**report},indent=2))


if __name__=='__main__':main()
