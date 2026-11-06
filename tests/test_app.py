import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from pbmc_platform.app.adapter import inspect, selected, qc_frames
from pbmc_platform.app.blobs import LocalBlobs, encode
from pbmc_platform.app.contracts import Configuration, Submission, Upload
from pbmc_platform.app.service import Service
from pbmc_platform.app.store import LocalStore
from pbmc_platform.app.worker import execute_stage, process_local, read_package
from pbmc_platform.app.runtime import recover, cleanup


def config(species='human', **overrides):
    values = dict(species=species, counts='X', gene_ids='gene_ids', gene_symbols='index', mito_mode='prefix',
                  mito_value='MT-' if species=='human' else 'mt-', min_genes=1,max_genes=4,max_mito_pct=50,
                  tissue='blood' if species=='human' else 'brain', reference_genome='test',attribution='synthetic fixture',
                  source='generated test fixture',single_sample=True,public_data=True)
    return Configuration(**{**values,**overrides})


@pytest.fixture
def sample(tmp_path):
    def create(species='human', layer=False):
        data=np.array([[0,2,3,0],[1,4,0,0],[5,0,0,0],[0,0,0,0],[0,1,1,1],[0,4,2,1]])
        symbols=[('MT-' if species=='human' else 'mt-')+'A','B','C','D']
        a=ad.AnnData(sparse.csr_matrix(data),obs=pd.DataFrame(index=[f'cell{i}' for i in range(6)]),
                    var=pd.DataFrame({'gene_ids':[f'id{i}' for i in range(4)], 'mito':[True,False,False,False]},index=symbols))
        if layer:
            a.layers['counts']=a.X.copy();a.X=a.X.astype(float)*0.3
        path=tmp_path/f'{species}-{layer}.h5ad';a.write_h5ad(path)
        return path
    return create


@pytest.fixture
def service(tmp_path):
    return Service(LocalStore(tmp_path/'state.sqlite'),LocalBlobs(tmp_path/'objects'))


def ready(service,path,owner='alice'):
    upload=service.upload(owner,Upload(filename=path.name,size=path.stat().st_size,public_data=True))
    service.blobs.write(upload['key'],path.read_bytes());service.finalize(owner,upload['id'])
    execute_stage(service,'uploads',upload['id'],'inspect-'+upload['id'],'inspect')
    return service.store.get('uploads/'+upload['id'])


def submit(service,upload,configuration=None,key='unique-key-1',owner='alice'):
    return service.submit(owner,Submission(upload_id=upload['id'],idempotency_key=key,configuration=configuration or config()))


@pytest.mark.parametrize('species',['human','mouse'])
@pytest.mark.parametrize('layer',[False,True])
def test_species_and_selected_layer(sample,species,layer):
    path=sample(species,layer)
    info=inspect(path);assert info['matrices']['X']['cells']==6
    c=config(species,counts='layer:counts' if layer else 'X')
    a,cells,genes,summary=qc_frames(path,c,'a'*32)
    assert summary.iloc[0].cells_total==6
    assert summary.iloc[0].cells_pass==4
    assert genes.is_mitochondrial.sum()==1
    assert cells.total_counts.tolist()==[5,5,5,0,3,7]


def test_normalized_counts_rejected(sample):
    with pytest.raises(ValueError,match='integer counts'):
        selected(sample(layer=True),config())


def test_boolean_mito(sample):
    assert selected(sample(),config(mito_mode='column',mito_value='mito')).var.app_mito.sum()==1


def test_missing_mito_rejected(sample):
    with pytest.raises(ValueError,match='No mitochondrial'):
        selected(sample(),config(mito_value='missing-'))


def test_hdf5_link_rejected(tmp_path):
    import h5py
    path=tmp_path/'link.h5ad'
    with h5py.File(path,'w') as f:f['external']=h5py.ExternalLink('other.h5','/X')
    with pytest.raises(ValueError,match='links'):
        inspect(path)


def test_hdf5_cycle_rejected(tmp_path):
    import h5py
    path=tmp_path/'cycle.h5ad'
    with h5py.File(path,'w') as f:f['cycle']=f['/']
    with pytest.raises(ValueError,match='cyclic'):
        inspect(path)


def test_ownership_idempotency_quota(service,sample):
    upload=ready(service,sample())
    with pytest.raises(LookupError):service.owned('uploads',upload['id'],'bob')
    first=submit(service,upload)
    assert submit(service,upload)['id']==first['id']
    with pytest.raises(ValueError,match='different configuration'):
        submit(service,upload,config(max_mito_pct=40))
    for i in range(4):submit(service,upload,key='another-key-'+str(i))
    with pytest.raises(ValueError,match='five runs'):submit(service,upload,key='over-quota')


def test_admission_serializes_and_recovery_fail_closed(service,sample):
    upload=ready(service,sample());a=submit(service,upload);b=submit(service,upload,key='second-key')
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda r:service.claim('runs',r['id'],'wf-'+r['id']),[a,b]))
    assert sorted(results)==[False,True]
    slot=service.store.get('control/slot')
    with pytest.raises(ValueError):recover(service,slot['owner'],slot['execution'])
    service.finish('runs',slot['owner'],slot['execution'],{'status':'failed'},release=False)
    with pytest.raises(ValueError,match='acknowledge'):recover(service,slot['owner'],slot['execution'])
    recover(service,slot['owner'],slot['execution'],True)
    assert service.store.get('control/slot') is None


def test_wrong_stage_cannot_run(service,sample):
    run=submit(service,ready(service,sample()))
    with pytest.raises(ValueError):execute_stage(service,'runs',run['id'],'unknown','knowledge')


def test_local_complete_compare_tamper_and_delete(service,sample):
    upload=ready(service,sample())
    a=submit(service,upload);process_local(service,'runs',a['id'])
    a=service.owned('runs',a['id'],'alice');assert a['status']=='succeeded'
    package=read_package(service,a);assert package['metrics']['cells_pass']==4
    from fastapi.testclient import TestClient
    from pbmc_platform.app.web import create_app
    with TestClient(create_app(service, local=True)) as client:
        url='/api/runs/'+a['id']+'/plots/counts.png'
        assert client.get(url).status_code==404  # Another owner's run is private.
        monkey_owner={**a,'owner':'local-researcher'}
        service.store.put('runs/'+a['id'],monkey_owner)
        response=client.get(url)
        assert response.status_code==200 and response.content.startswith(b'\x89PNG')
        assert response.headers['content-type']=='image/png'
        assert 'attachment' in response.headers['content-disposition']
        assert client.get(url+'?scale=unsupported').status_code==404
        assert client.get('/api/runs/'+a['id']+'/plots/counts.html').status_code==404
        service.store.put('runs/'+a['id'],a)

    assert package['dbt']['models']==6 and package['dbt']['tests']==30
    b=submit(service,upload,config(max_mito_pct=10),key='second-policy');process_local(service,'runs',b['id'])
    comparison=service.compare('alice',a['id'],b['id'])
    assert comparison['only_left']==1 and comparison['only_right']==0
    with pytest.raises(LookupError):service.compare('bob',a['id'],b['id'])
    path=service.blobs.path(f'runs/{a["id"]}/published/knowledge.json');original=path.read_bytes();path.write_bytes(b'{}')
    with pytest.raises(ValueError):read_package(service,a)
    path.write_bytes(original)
    service.store.put('uploads/'+upload['id'],{**service.store.get('uploads/'+upload['id']),'status':'deleting'})
    assert cleanup(service)['deleted_uploads']==1
    assert not path.exists()
    assert service.store.get('runs/'+a['id'])['status']=='deleted'


def test_inspection_failure_keeps_slot(service,tmp_path):
    path=tmp_path/'bad.h5ad';path.write_bytes(b'bad input')
    u=service.upload('alice',Upload(filename=path.name,size=path.stat().st_size,public_data=True))
    service.blobs.write(u['key'],path.read_bytes());service.finalize('alice',u['id'])
    with pytest.raises(Exception):execute_stage(service,'uploads',u['id'],'inspection','inspect')
    assert service.store.get('control/slot')['owner']==u['id']
    assert service.store.get('uploads/'+u['id'])['status']=='failed'


@pytest.mark.parametrize('port', [None, '8769'])
def test_http_security_and_submission(service,sample,monkeypatch,port):
    from fastapi.testclient import TestClient
    from pbmc_platform.app.web import create_app
    monkeypatch.delenv('SCRNA_ORIGIN', raising=False)
    if port is None:
        monkeypatch.delenv('PORT', raising=False)
    else:
        monkeypatch.setenv('PORT', port)
    origin = 'http://127.0.0.1:' + (port or '8768')
    monkeypatch.setattr('pbmc_platform.app.web.subprocess.Popen',lambda *a,**k:None)
    app=create_app(service,local=True)
    with TestClient(app) as client:
        assert client.get('/',headers={'Host':'evil.example'}).status_code==400
        assert client.get('/').status_code==200
        assert client.get('/api/session').json()['signed_in']
        assert client.post('/api/uploads',json={}).status_code==403
        headers={'Origin':origin,'X-CSRF-Token':client.cookies.get('csrf')}
        path=sample()
        response=client.post('/api/uploads',json={'filename':path.name,'size':path.stat().st_size,'public_data':True},headers=headers)
        assert response.status_code==200
        upload=response.json();content=client.put(upload['upload_url'],content=path.read_bytes(),headers=headers)
        assert content.status_code==200
        assert client.post('/api/uploads/'+upload['upload']['id']+'/finalize',json={},headers=headers).status_code==200
        assert client.get('/api/runs/'+'a'*32).status_code==404
        assert client.post('/internal/dispatch',json={},headers=headers).status_code==404


def test_cloud_refuses_development(monkeypatch,service):
    from pbmc_platform.app.web import create_app
    monkeypatch.setenv('K_SERVICE','any-service')
    with pytest.raises(RuntimeError):create_app(service,local=True)


@pytest.mark.parametrize('species',['human','mouse'])
def test_duplicate_gene_ids_rejected(sample,species):
    path=sample(species);a=ad.read_h5ad(path);a.var['gene_ids']=['same']*a.n_vars;a.write_h5ad(path)
    with pytest.raises(ValueError,match='unique'):selected(path,config(species))


def test_dense_input_rejected(sample):
    path=sample();a=ad.read_h5ad(path);a.X=a.X.toarray();a.write_h5ad(path)
    with pytest.raises(ValueError,match='No supported sparse'):inspect(path)


def test_concurrent_upload_limit(service,sample):
    path=sample()
    def create(i):
        try:return service.upload('alice',Upload(filename=path.name,size=path.stat().st_size,public_data=True))
        except ValueError:return None
    with ThreadPoolExecutor(8) as pool: results=list(pool.map(create,range(16)))
    assert sum(x is not None for x in results)==10


def test_submit_after_delete_is_rejected(service,sample):
    upload=ready(service,sample())
    service.store.put('uploads/'+upload['id'],{**upload,'status':'deleting'})
    with pytest.raises(LookupError):submit(service,upload)


def test_idempotent_parallel_submission(service,sample):
    upload=ready(service,sample())
    with ThreadPoolExecutor(5) as pool: runs=list(pool.map(lambda _:submit(service,upload),range(5)))
    assert len({r['id'] for r in runs})==1


def test_expiry_hides_upload(service,sample):
    upload=ready(service,sample());service.store.put('uploads/'+upload['id'],{**upload,'expires_at':'2000-01-01T00:00:00+00:00'})
    with pytest.raises(LookupError,match='expired'):service.owned('uploads',upload['id'],'alice')
    assert cleanup(service)['deleted_uploads']==1


def test_cloud_auth_allowlist_and_csrf(service,monkeypatch):
    from fastapi.testclient import TestClient
    import pbmc_platform.app.web as web
    monkeypatch.setenv('SCRNA_ALLOWED_EMAILS','invited@example.com')
    monkeypatch.setenv('SCRNA_ORIGIN','https://app.example.com')
    class Auth:
        def verify_session_cookie(self,cookie,check_revoked):
            if cookie=='invited':return {'uid':'invite','email':'invited@example.com','email_verified':True}
            if cookie=='outsider':return {'uid':'other','email':'other@example.com','email_verified':True}
            raise ValueError('Bad session')
    monkeypatch.setattr(web,'firebase',lambda:Auth())
    with TestClient(web.create_app(service,local=False),base_url='https://app.example.com') as client:
        assert client.get('/').status_code==200
        assert client.get('/api/runs').status_code==401
        client.cookies.set('session','outsider');assert client.get('/api/runs').status_code==401
        client.cookies.set('session','invited');assert client.get('/api/runs').status_code==200
        assert client.post('/api/uploads',json={}).status_code==403
        assert client.get('/api/runs/'+'a'*32).status_code==404


@pytest.mark.parametrize('scripts_relative', ['Python/Scripts', '.venv/Scripts', '.venv/bin'])
def test_dbt_uses_active_python_scripts_directory(tmp_path, monkeypatch, scripts_relative):
    import os
    import pbmc_platform.app.worker as worker

    scripts = tmp_path / scripts_relative
    project = tmp_path / 'private dbt project'
    # The scripts folder need not be next to python.exe (standard Windows Python).
    monkeypatch.setattr(worker.sys, 'executable', str(tmp_path / 'Python' / 'python.exe'))
    monkeypatch.setattr(worker.sysconfig, 'get_path', lambda name: str(scripts) if name == 'scripts' else None)
    monkeypatch.setattr(worker, 'database', lambda service: tmp_path / 'analytics.duckdb')
    monkeypatch.setattr(worker, 'dbt_project', lambda *args: project)

    class CommandCaptured(Exception):
        pass

    def capture(command, **kwargs):
        assert command == [str(scripts / ('dbt.exe' if os.name == 'nt' else 'dbt')),
                           'build', '--project-dir', str(project), '--profiles-dir', str(project),
                           '--target', 'local', '--no-partial-parse']
        assert not kwargs.get('shell', False)
        raise CommandCaptured

    monkeypatch.setattr(worker.subprocess, 'run', capture)
    with pytest.raises(CommandCaptured):
        worker.dbt_stage(None, {'release_id': 'a' * 64}, tmp_path)


def test_dbt_uses_writable_private_template_copy(tmp_path,monkeypatch):
    import shutil
    import pbmc_platform.app.worker as worker
    original=shutil.copytree
    def readonly_copy(*args,**kwargs):
        result=original(*args,**kwargs)
        if Path(args[1]).name=='dbt':
            (Path(args[1])/'macros').chmod(0o500)
            Path(args[1]).chmod(0o500)
        return result
    monkeypatch.setattr(worker.shutil,'copytree',readonly_copy)
    destination=worker.dbt_project(tmp_path,'a'*64,tmp_path/'analytics.duckdb')
    assert (destination/'macros/generate_alias_name.sql').is_file()
    assert (destination/'profiles.yml').is_file()


@pytest.mark.parametrize('over_cap',[False,True])
def test_cloud_staging_views_use_capped_query(monkeypatch,over_cap):
    from types import SimpleNamespace
    from google.cloud import bigquery
    import pbmc_platform.app.worker as worker
    calls=[]
    class Client:
        project='pbmc-okf-portfolio'
        def get_table(self,table):return SimpleNamespace(table_type='VIEW',schema=[bigquery.SchemaField('cell_id','STRING')])
        def list_rows(self,table):raise AssertionError('BigQuery cannot list rows of a view')
        def query(self,sql,location,job_config):
            calls.append(job_config)
            assert 'scrna_app_analytics.stg_cells_r_' in sql
            assert job_config.maximum_bytes_billed==100000000
            if job_config.dry_run:return SimpleNamespace(total_bytes_processed=100000001 if over_cap else 100)
            return SimpleNamespace(result=lambda timeout:[{'cell_id':'checked'}])
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT','pbmc-okf-portfolio')
    monkeypatch.setattr(bigquery,'Client',lambda **kwargs:Client())
    service=SimpleNamespace(blobs=SimpleNamespace())
    if over_cap:
        with pytest.raises(ValueError,match='scan cap'):worker.analytics_rows(service,{'release_id':'a'*64},'stg_cells')
        assert len(calls)==1
    else:
        frame=worker.analytics_rows(service,{'release_id':'a'*64},'stg_cells')
        assert frame.cell_id.tolist()==['checked'] and len(calls)==2


def test_counts_do_not_silently_lose_integer_precision(sample):
    path=sample();a=ad.read_h5ad(path);a.X=a.X.astype(np.int64);a.X.data[0]=2**53+1;a.write_h5ad(path)
    with pytest.raises(ValueError,match='precision'):selected(path,config())


def test_hdf5_external_raw_storage_rejected(tmp_path):
    import h5py
    path=tmp_path/'external-storage.h5ad'
    with h5py.File(path,'w') as f:
        f.create_dataset('external',shape=(10,),dtype='i4',external=[('outside.bin',0,40)])
    with pytest.raises(ValueError,match='external HDF5'):inspect(path)


@pytest.mark.parametrize('state',['ACTIVE','FAILED','SUCCEEDED'])
@pytest.mark.parametrize('paused',[False,True])
def test_cloud_finalization_requires_terminal_platform_success(service,sample,monkeypatch,state,paused):
    import pbmc_platform.app.runtime as runtime
    import pbmc_platform.app.worker as worker
    upload=ready(service,sample());run=submit(service,upload)
    service.claim('runs',run['id'],'workflow-owner')
    service.finish('runs',run['id'],'workflow-owner',{'status':'published'},release=False)
    service.store.put('control/admission',{'paused':paused})
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'state':state}
    class API:
        def get(self,url,timeout):return Response()
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT','pbmc-okf-portfolio')
    monkeypatch.setenv('SCRNA_WORKFLOW','scrna-app-pipeline')
    monkeypatch.setattr(runtime,'authorized_session',lambda:API())
    checked=[]
    monkeypatch.setattr(worker,'read_package',lambda *args:checked.append(True))
    runtime.dispatch_cloud(service)
    result=service.store.get('runs/'+run['id'])
    if state=='SUCCEEDED':
        assert result['status']=='succeeded' and service.store.get('control/slot') is None and checked
    elif state=='FAILED':
        assert result['status']=='failed' and service.store.get('control/slot') is not None and not checked
    else:
        assert result['status']=='published' and service.store.get('control/slot') is not None and not checked


def test_malformed_sparse_indices_rejected_before_computation(sample):
    import h5py
    path=sample()
    with h5py.File(path,'r+') as f:
        f['X']['indices'][0]=-1
    with pytest.raises(ValueError):selected(path,config())


def test_duplicate_completion_does_not_delete_another_runs_guard(service,sample):
    upload=ready(service,sample());a=submit(service,upload);b=submit(service,upload,key='second-run-key')
    assert service.claim('runs',a['id'],'owner-a')
    service.finish('runs',a['id'],'owner-a',{'status':'succeeded'})
    assert service.claim('runs',b['id'],'owner-b')
    result=service.finish('runs',a['id'],'owner-a',{'status':'succeeded'})
    assert result['status']=='succeeded'
    assert service.store.get('control/slot')['owner']==b['id']


def test_cleanup_retries_after_some_runs_are_already_deleted(service,sample,monkeypatch):
    upload=ready(service,sample());a=submit(service,upload);b=submit(service,upload,key='retention-retry')
    service.store.put('uploads/'+upload['id'],{**service.store.get('uploads/'+upload['id']),'status':'deleting'})
    original=service.blobs.delete_prefix
    def fail_source(prefix):
        if prefix.startswith('uploads/'):
            raise RuntimeError('Transient source deletion failure')
        return original(prefix)
    monkeypatch.setattr(service.blobs,'delete_prefix',fail_source)
    with pytest.raises(RuntimeError):cleanup(service)
    assert service.store.get('runs/'+a['id'])['status']=='deleted'
    assert service.store.get('uploads/'+upload['id'])['status']=='deleting'
    monkeypatch.setattr(service.blobs,'delete_prefix',original)
    assert cleanup(service)['deleted_uploads']==1
    assert service.store.get('runs/'+b['id'])['status']=='deleted'
    assert service.store.get('uploads/'+upload['id'])['status']=='deleted'


def test_queue_status_reports_failed_guard_without_exposing_owner(service):
    from fastapi.testclient import TestClient
    from pbmc_platform.app.web import create_app
    with TestClient(create_app(service, local=True)) as client:
        assert client.get('/api/queue-status').json() == {'blocked': False, 'paused': False}
        service.store.put('control/slot', {'kind': 'runs', 'owner': 'private-other-user-run'})
        service.store.put('runs/private-other-user-run', {'status': 'failed', 'error': 'private details'})
        response = client.get('/api/queue-status')
        assert response.json() == {'blocked': True, 'paused': False}
        assert 'private' not in response.text
        service.store.put('runs/private-other-user-run', {'status': 'running'})
        service.store.put('control/admission', {'paused': True})
        assert client.get('/api/queue-status').json() == {'blocked': False, 'paused': True}


def test_clear_history_is_owner_scoped_preserves_data_and_blocked_operations(service,sample):
    upload=ready(service,sample())
    entries=[]
    for index,status in enumerate(['succeeded','failed','cancelled','queued','running']):
        record=submit(service,upload,key='history-key-'+str(index))
        record.update(status=status)
        service.store.put('runs/'+record['id'],record)
        entries.append(record)
    other=ready(service,sample(),owner='bob')
    bob=submit(service,other,owner='bob',key='bob-history-key')
    service.store.put('runs/'+bob['id'],{**bob,'status':'succeeded'})
    slot={'kind':'runs','owner':entries[1]['id'],'execution':'failed-operation'}
    service.store.put('control/slot',slot)
    quotas=service.store.scan('quotas');source=service.blobs.read(upload['key'])
    assert service.clear_history('alice')=={'cleared':2,'retained_blocked':1}
    for index,record in enumerate(entries):
        current=service.store.get('runs/'+record['id'])
        assert current['status']==record['status']
        assert current.get('history_hidden',False)==(index in [0,2])
    assert not service.store.get('runs/'+bob['id']).get('history_hidden')
    assert service.store.get('control/slot')==slot
    assert service.store.scan('quotas')==quotas
    assert service.blobs.read(upload['key'])==source
    assert service.clear_history('alice')=={'cleared':0,'retained_blocked':1}


def test_history_clear_api_requires_csrf_and_keeps_run_available(service,sample):
    from fastapi.testclient import TestClient
    from pbmc_platform.app.web import create_app
    upload=ready(service,sample(),owner='local-researcher')
    run=submit(service,upload,owner='local-researcher')
    service.store.put('runs/'+run['id'],{**run,'status':'cancelled'})
    with TestClient(create_app(service,local=True)) as client:
        client.get('/')
        assert client.post('/api/history/clear',json={}).status_code==403
        headers={'Origin':'http://127.0.0.1:8768','X-CSRF-Token':client.cookies.get('csrf')}
        response=client.post('/api/history/clear',json={},headers=headers)
        assert response.status_code==200 and response.json()['cleared']==1
        saved=client.get('/api/runs/'+run['id']).json()
        assert saved['history_hidden'] and saved['status']=='cancelled'


def test_comparison_requires_distinct_runs(service,sample):
    upload=ready(service,sample());run=submit(service,upload)
    with pytest.raises(ValueError,match='two different'):
        service.compare('alice',run['id'],run['id'])


def test_example_button_reuses_verified_copy_and_skips_failed_or_deleted(service,sample,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from pbmc_platform.app import web
    from pbmc_platform.app.blobs import sha
    data=sample().read_bytes()
    root=tmp_path/'example-root'
    (root/'examples').mkdir(parents=True)
    (root/'data'/'app-examples').mkdir(parents=True)
    spec={'prepared_filename':'human_example.h5ad','prepared_sha256':sha(data)}
    (root/'examples'/'app-datasets.json').write_text(json.dumps({'human':spec}))
    (root/'data'/'app-examples'/'human_example.h5ad').write_bytes(data)
    monkeypatch.setattr(web,'ROOT',root)
    monkeypatch.setattr(web,'LOCAL_PROCESS',None)
    monkeypatch.setattr(web.subprocess,'Popen',lambda *args,**kwargs:None)
    service.store.put('uploads/'+'d'*32,{'id':'d'*32,'owner':'local-researcher',
        'status':'deleted','expires_at':'2099-01-01T00:00:00+00:00'})
    with TestClient(web.create_app(service,local=True)) as client:
        client.get('/')
        headers={'Origin':'http://127.0.0.1:8768','X-CSRF-Token':client.cookies.get('csrf')}
        first=client.post('/api/examples/human',json={},headers=headers)
        assert first.status_code==200,first.text
        second=client.post('/api/examples/human',json={},headers=headers)
        assert second.json()['id']==first.json()['id']
        assert len(client.get('/api/uploads').json())==1
        record=first.json();service.store.put('uploads/'+record['id'],{**record,'status':'failed'})
        third=client.post('/api/examples/human',json={},headers=headers)
        assert third.json()['id']!=record['id']


def test_local_owner_can_submit_more_than_five_runs_with_accounting(service,sample):
    owner_service=Service(service.store,service.blobs,unlimited_run_owners={'local-researcher'})
    upload=ready(owner_service,sample(),owner='local-researcher')
    runs=[submit(owner_service,upload,owner='local-researcher',key='owner-test-key-'+str(i)) for i in range(8)]
    assert len({r['id'] for r in runs})==8
    assert owner_service.run_limit('local-researcher') is None
    assert owner_service.store.scan('quotas')[0]['count']==8
    assert submit(owner_service,upload,owner='local-researcher',key='owner-test-key-7')['id']==runs[-1]['id']
    assert owner_service.store.scan('quotas')[0]['count']==8
    # An owner name alone does not bypass the default hosted-service limit.
    with pytest.raises(ValueError,match='five runs'):
        submit(service,upload,owner='local-researcher',key='no-configured-exemption')


def test_local_factory_exemption_does_not_apply_to_cloud(tmp_path,monkeypatch):
    from pbmc_platform.app import web
    monkeypatch.delenv('K_SERVICE',raising=False)
    monkeypatch.setenv('SCRNA_MODE','local')
    monkeypatch.setenv('SCRNA_LOCAL_ROOT',str(tmp_path/'local-app'))
    assert web.make_service().run_limit('local-researcher') is None
    assert web.make_service().run_limit('other-user')==5
    monkeypatch.setenv('SCRNA_MODE','cloud')
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT','test-project')
    monkeypatch.setenv('SCRNA_BUCKET','test-bucket')
    monkeypatch.setattr(web,'FirestoreStore',lambda *args:object())
    monkeypatch.setattr(web,'GCSBlobs',lambda *args:object())
    assert web.make_service().run_limit('local-researcher')==5
