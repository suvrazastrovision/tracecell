"""Synthetic boundary benchmark; does not establish biological validity or cloud SLA."""
import json
from pathlib import Path
import time
import threading
import uuid


def main():
    import anndata as ad
    import numpy as np
    import pandas as pd
    import psutil
    from scipy import sparse
    from pbmc_platform.app.blobs import LocalBlobs
    from pbmc_platform.app.contracts import Configuration, Submission, Upload
    from pbmc_platform.app.service import Service
    from pbmc_platform.app.store import LocalStore
    from pbmc_platform.app.worker import execute_stage, process_local, read_package
    root=Path('artifacts/app-boundary')/uuid.uuid4().hex;root.mkdir(parents=True)
    # 500 distinct entries/cell: exactly 10,000,000 entries at the dimension caps.
    indices=((np.arange(20000,dtype=np.int32)[:,None]*17+np.arange(500,dtype=np.int32))%40000).ravel()
    matrix=sparse.csr_matrix((np.ones(10000000,dtype=np.int16),indices,np.arange(0,10000001,500,dtype=np.int32)),shape=(20000,40000))
    sample=ad.AnnData(matrix,obs=pd.DataFrame(index=['cell'+str(i) for i in range(20000)]),
                     var=pd.DataFrame({'gene_ids':['gene'+str(i) for i in range(40000)]},index=['MT-GENE0',*['G'+str(i) for i in range(1,40000)]]))
    path=root/'boundary.h5ad';sample.write_h5ad(path,compression='gzip');del sample,matrix,indices
    config=Configuration(species='human',counts='X',gene_ids='gene_ids',gene_symbols='index',mito_mode='prefix',mito_value='MT-',
                         min_genes=200,max_genes=600,max_mito_pct=5,tissue='synthetic',reference_genome='synthetic',
                         attribution='Generated benchmark fixture',source='ops.check_app_limits',single_sample=True,public_data=True)
    service=Service(LocalStore(root/'state.sqlite'),LocalBlobs(root/'objects'))
    stop=threading.Event();peak=[0]
    def monitor():
        p=psutil.Process()
        while not stop.wait(.2):
            total=0
            for child in [p,*p.children(recursive=True)]:
                try:total+=child.memory_info().rss
                except psutil.Error:pass
            peak[0]=max(total,peak[0])
    thread=threading.Thread(target=monitor,daemon=True);thread.start();start=time.monotonic()
    report={'status':'running','scope':'Synthetic local upper-bound input; not a cloud capacity or cost guarantee.'}
    try:
        upload=service.upload('boundary',Upload(filename=path.name,size=path.stat().st_size,public_data=True))
        service.blobs.write(upload['key'],path.read_bytes());service.finalize('boundary',upload['id'])
        execute_stage(service,'uploads',upload['id'],'inspect-'+upload['id'],'inspect')
        run=service.submit('boundary',Submission(upload_id=upload['id'],idempotency_key='boundary-check',configuration=config))
        process_local(service,'runs',run['id']);run=service.owned('runs',run['id'],'boundary');package=read_package(service,run)
        assert package['metrics']['cells_total']==20000 and package['metrics']['features_total']==40000
        assert package['metrics']['cells_pass']==20000 and package['metrics']['total_counts']==10000000
        report.update(status='success',metrics=package['metrics'],dbt=package['dbt'],input_bytes=path.stat().st_size,nnz=10000000)
    finally:
        stop.set();thread.join();report.update(seconds_observed=round(time.monotonic()-start,2),peak_process_tree_rss_bytes=peak[0])
        (root/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(json.dumps({'report':str(root/'report.json'),**report},indent=2))


if __name__=='__main__':main()
