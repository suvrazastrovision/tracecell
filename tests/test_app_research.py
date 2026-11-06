from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pbmc_platform.app.blobs import LocalBlobs, encode, sha
from pbmc_platform.app.contracts import Configuration
from pbmc_platform.app.service import Service
from pbmc_platform.app.store import LocalStore
from pbmc_platform.app.web import create_app
from pbmc_platform.app.research import (MAX_PDF, PaperRule, Reference, compare_reference,
                                       paper_record, parse_pdf, remove_paper, save_paper)
from pbmc_platform.app import knowledge_chat as chat


TEXT = ('Synthetic methods fixture, not a research standard. Human blood cells were retained with '
        'at least 200 detected genes, fewer than 4000 genes and mitochondrial percentage below 10. '
        'These thresholds are for a test dataset only.')


@pytest.fixture
def pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(('BT /F1 10 Tf 40 700 Td (' + TEXT + ') Tj ET').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO(); writer.write(output)
    return output.getvalue()


@pytest.fixture
def verified(tmp_path, monkeypatch):
    monkeypatch.setenv('SCRNA_LLM_PROVIDER', 'disabled')
    service = Service(LocalStore(tmp_path / 'state.sqlite'), LocalBlobs(tmp_path / 'objects'))
    identity = 'a' * 32
    config = Configuration(species='human', counts='X', gene_ids='gene_ids', gene_symbols='index',
        mito_mode='prefix', mito_value='MT-', min_genes=200, max_genes=2500, max_mito_pct=5,
        tissue='blood', reference_genome='hg19', source='synthetic test fixture', attribution='test',
        single_sample=True, public_data=True).model_dump()
    package = {'run_id': identity, 'configuration': config,
        'metrics': {'cells_total': 4, 'cells_pass': 1, 'cells_fail': 3, 'features_total': 4000},
        'exclusions': {'low_genes': 2}, 'dbt': {'models': 6, 'tests': 30},
        'limitations': ['Synthetic test fixture'], 'passing_barcodes': ['private-test-barcode']}
    knowledge = encode(package)
    objects = {}
    outputs = {'knowledge.json': knowledge, 'validation.json': encode({'status': 'success', 'knowledge_sha256': sha(knowledge)}),
        'plots.json': encode({'total_counts': [0,100,200,300], 'detected_genes': [0,100,500,3000],
                             'mitochondrial_pct': [None,1,4,8], 'passing': [False,False,True,False]})}
    for name, data in outputs.items():
        objects[name] = service.blobs.write(f'runs/{identity}/published/{name}', data)
    from pbmc_platform.app.contracts import digest
    marker = {'run_id': identity, 'release_id': 'b'*64, 'package_id': digest(objects), 'objects': objects}
    publication = service.blobs.write(f'runs/{identity}/published/publication.json', encode(marker))
    service.store.put('runs/'+identity, {'id': identity, 'owner': 'local-researcher', 'status': 'succeeded',
        'expires_at': '2099-01-01T00:00:00+00:00', 'release_id': 'b'*64, 'publication': publication})
    return service, identity, package


def reference(**overrides):
    values = {'rules': [PaperRule(field=field, value=value, operator=operator, page=1, quote=TEXT)
        for field,value,operator in [('min_genes',200,'>='),('max_genes',4000,'<'),('max_mito_pct',10,'<')]],
        'species': 'human', 'tissue': 'blood', 'confirmed': True}
    return Reference(**{**values, **overrides})


def test_real_pdf_parser_and_private_paper_lifecycle(verified, pdf):
    service, run, _ = verified
    pages = parse_pdf(pdf)
    assert pages[0]['page'] == 1 and TEXT in pages[0]['text']
    paper = save_paper(service, 'local-researcher', run, 'methods.pdf', pdf)
    assert paper['page_count'] == 1
    with pytest.raises(LookupError): paper_record(service, 'outsider', run, paper['id'])
    result = compare_reference(service, 'local-researcher', run, paper['id'], reference())
    assert (result['current_pass'],result['reference_pass'],result['both_pass'],result['only_reference']) == (1,2,1,1)
    remove_paper(service, 'local-researcher', run, paper['id'])
    with pytest.raises(LookupError): paper_record(service, 'local-researcher', run, paper['id'])
    assert not any(p.is_file() for p in service.blobs.path(f'runs/{run}/papers/{paper["id"]}').rglob('*'))


def test_paper_rules_require_exact_citations_and_numeric_values(verified, pdf):
    service, run, _ = verified
    paper = save_paper(service, 'local-researcher', run, 'methods.pdf', pdf)
    for override in [{'quote':'This quote was invented by a model'}, {'page':2}, {'value':9}]:
        rule = PaperRule(field='max_mito_pct',value=10,operator='<',page=1,quote=TEXT).model_copy(update=override)
        with pytest.raises(ValueError):
            compare_reference(service,'local-researcher',run,paper['id'],reference(rules=[rule]))
    partial=reference(rules=reference().rules[:1],species='mouse',tissue='brain')
    result=compare_reference(service,'local-researcher',run,paper['id'],partial)
    assert any('Partial' in x for x in result['warnings'])
    assert any('species' in x for x in result['warnings'])
    assert any('tissue' in x for x in result['warnings'])
    with pytest.raises(ValueError): Reference(**{**reference().model_dump(),'confirmed':False})


def test_pdf_limits_and_paper_quota(verified, pdf):
    with pytest.raises(ValueError): parse_pdf(b'not a PDF')
    with pytest.raises(ValueError): parse_pdf(b'%PDF-' + b'x'*MAX_PDF)
    blank=PdfWriter();blank.add_blank_page(612,792);out=BytesIO();blank.write(out)
    with pytest.raises(ValueError,match='No usable text'):parse_pdf(out.getvalue())
    service,run,_=verified
    papers=[save_paper(service,'local-researcher',run,'methods.pdf',pdf) for _ in range(3)]
    with pytest.raises(ValueError,match='three papers'):save_paper(service,'local-researcher',run,'methods.pdf',pdf)
    remove_paper(service,'local-researcher',run,papers[0]['id'])
    assert save_paper(service,'local-researcher',run,'replacement.pdf',pdf)['page_count']==1


def test_pdf_larger_than_previous_limit_is_readable(pdf):
    # Trailing PDF whitespace enlarges the upload without changing its methods text.
    larger = pdf + b' ' * (6 * 1024 * 1024)
    assert parse_pdf(larger)[0]['text'] == TEXT


def test_http_paper_size_boundary(verified, monkeypatch):
    import pbmc_platform.app.research as research
    service, run, _ = verified
    assert MAX_PDF == 100_000_000
    received = []
    def capture(service, owner, run_id, filename, data):
        received.append(len(data))
        return {'size': len(data)}
    # Isolate HTTP body admission from PDF validity and storage, tested separately.
    monkeypatch.setattr(research, 'save_paper', capture)
    with TestClient(create_app(service, local=True)) as client:
        client.get('/')
        headers = {'Origin': 'http://127.0.0.1:8768', 'X-CSRF-Token': client.cookies.get('csrf'),
                   'Content-Type': 'application/pdf'}
        payload = b'x' * MAX_PDF
        accepted = client.post(f'/api/runs/{run}/papers', content=payload, headers=headers)
        assert accepted.status_code == 200
        assert accepted.json()['size'] == MAX_PDF
        rejected = client.post(f'/api/runs/{run}/papers', content=payload + b'x', headers=headers)
        assert rejected.status_code == 400
        assert '100 MB' in rejected.text
        assert received == [MAX_PDF]


def test_model_disabled_is_honest_and_does_not_share_data(verified):
    service,_,package=verified
    answer=chat.ask(service,'local-researcher',package,chat.Question(question='Why were cells excluded?'))
    assert answer['mode']=='evidence_lookup' and answer['sources'][0]['id']=='evidence:exclusions'
    assert not service.store.scan('assistant_quotas')
    assert 'private-test-barcode' not in json.dumps(chat.sources_for(package))


def test_model_consent_and_citation_validation(verified, monkeypatch):
    service,_,package=verified
    monkeypatch.setenv('SCRNA_LLM_PROVIDER','openai');monkeypatch.setenv('OPENAI_API_KEY','unit-test-placeholder')
    with pytest.raises(ValueError,match='Confirm sharing'):
        chat.structured_call(service,'local-researcher','instructions',{},chat.ModelAnswer,False)
    assert not service.store.scan('assistant_quotas')
    monkeypatch.setattr(chat,'structured_call',lambda *args:chat.ModelAnswer(answer='Invented citation',source_ids=['other-run:metrics'],insufficient_evidence=False))
    with pytest.raises(ValueError,match='unsupported citations'):
        chat.ask(service,'local-researcher',package,chat.Question(question='How many pass?',allow_external=True))


def test_openai_adapter_uses_no_storage_and_pinned_context(verified, monkeypatch):
    import openai
    service,_,package=verified;captured={}
    monkeypatch.setenv('SCRNA_LLM_PROVIDER','openai');monkeypatch.setenv('OPENAI_API_KEY','unit-test-placeholder')
    class Client:
        def __init__(self,**kwargs):self.responses=self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def parse(self,**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=chat.ModelAnswer(answer='One cell passed.',source_ids=['evidence:metrics'],insufficient_evidence=False))
    monkeypatch.setattr(openai,'OpenAI',Client)
    answer=chat.ask(service,'local-researcher',package,chat.Question(question='How many cells pass?',allow_external=True))
    assert answer['mode']=='model_interpretation'
    assert captured['store'] is False and captured['model']=='gpt-5-nano'
    assert 'private-test-barcode' not in captured['input'] and 'tools' not in captured


def test_research_http_upload_comparison_and_csrf(verified, pdf):
    service,run,_=verified
    with TestClient(create_app(service,local=True)) as client:
        client.get('/');headers={'Origin':'http://127.0.0.1:8768','X-CSRF-Token':client.cookies.get('csrf'),'Content-Type':'application/pdf'}
        assert client.post(f'/api/runs/{run}/papers',content=pdf).status_code==403
        response=client.post(f'/api/runs/{run}/papers?filename=methods.pdf',content=pdf,headers=headers)
        assert response.status_code==200,response.text
        paper=response.json()['id'];prefix=f'/api/runs/{run}/papers/{paper}'
        assert client.get(prefix).json()['pages'][0]['page']==1
        assert client.get(prefix+'/pdf').content.startswith(b'%PDF-')
        headers['Content-Type']='application/json'
        response=client.post(prefix+'/compare',json=reference().model_dump(),headers=headers)
        assert response.status_code==200 and response.json()['reference_pass']==2
        question={'question':'Compare my paper reference with this QC run','paper_id':paper,'reference':reference().model_dump()}
        response=client.post(f'/api/runs/{run}/chat',json=question,headers=headers)
        assert response.status_code==200 and response.json()['sources'][0]['id']=='reference:comparison'
        assert client.delete(prefix,headers=headers).status_code==200
        assert client.get(prefix).status_code==404


def test_encrypted_and_overlong_pdfs_are_rejected():
    writer=PdfWriter();writer.add_blank_page(612,792);writer.encrypt('test-password')
    output=BytesIO();writer.write(output)
    with pytest.raises(ValueError,match='Password'):parse_pdf(output.getvalue())
    writer=PdfWriter()
    for _ in range(41):writer.add_blank_page(612,792)
    output=BytesIO();writer.write(output)
    with pytest.raises(ValueError,match='1 to 40'):parse_pdf(output.getvalue())


def test_model_quota_is_bounded_and_owner_scoped(verified):
    service,_,_=verified
    for _ in range(30):chat.reserve_call(service,'alice')
    with pytest.raises(ValueError,match='Daily limit'):chat.reserve_call(service,'alice')
    chat.reserve_call(service,'bob')


def test_tampered_paper_text_is_rejected(verified,pdf):
    service,run,_=verified
    paper=save_paper(service,'local-researcher',run,'methods.pdf',pdf)
    record=paper_record(service,'local-researcher',run,paper['id'])
    service.blobs.path(record['text']['key']).write_text('{"pages": []}')
    with pytest.raises(ValueError):compare_reference(service,'local-researcher',run,paper['id'],reference())


def test_cleanup_deletes_run_papers_and_metadata(verified,pdf):
    from pbmc_platform.app.runtime import cleanup
    service,run,_=verified
    paper=save_paper(service,'local-researcher',run,'methods.pdf',pdf)
    upload='c'*32
    service.store.put('uploads/'+upload,{'id':upload,'owner':'local-researcher','status':'deleting',
        'expires_at':'2099-01-01T00:00:00+00:00','run_ids':[run]})
    current=service.store.get('runs/'+run)
    service.store.put('runs/'+run,{**current,'upload_id':upload})
    assert cleanup(service)['deleted_uploads']==1
    assert service.store.get('papers/'+paper['id'])['status']=='deleted'
    assert service.store.get('paper_indexes/'+run) is None


def test_local_model_adapter_uses_loopback_and_schema(verified,monkeypatch):
    import httpx
    service,_,package=verified;captured={}
    monkeypatch.setenv('SCRNA_LLM_PROVIDER','ollama');monkeypatch.setenv('SCRNA_OLLAMA_URL','http://127.0.0.1:11434')
    class Client:
        def __init__(self,**kwargs):assert kwargs['trust_env'] is False
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,json):
            captured.update(url=url,payload=json)
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'message':{'content':chat.ModelAnswer(
                answer='One cell passes.',source_ids=['evidence:metrics'],insufficient_evidence=False).model_dump_json()}})
    monkeypatch.setattr(httpx,'Client',Client)
    result=chat.ask(service,'local-researcher',package,chat.Question(question='How many cells pass?'))
    assert result['mode']=='model_interpretation' and captured['url'].startswith('http://127.0.0.1:11434/')
    assert captured['payload']['stream'] is False and captured['payload']['format']['type']=='object'


def test_high_resolution_plot_preview_is_inline_and_download_remains_attachment(verified):
    service,run,_=verified
    with TestClient(create_app(service,local=True)) as client:
        url=f'/api/runs/{run}/plots/counts.png'
        preview=client.get(url+'?preview=true')
        assert preview.status_code==200 and preview.content.startswith(b'\x89PNG')
        assert preview.headers['content-disposition'].startswith('inline;')
        assert client.get(url).headers['content-disposition'].startswith('attachment;')
        assert "style-src 'self'" in preview.headers['content-security-policy']
