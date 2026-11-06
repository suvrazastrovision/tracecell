"""Prepare small, attributed human/mouse count inputs; never normalize counts."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen, Request

DATASETS = {
    'human_pbmc3k': {'species': 'human', 'url': 'https://falexwolf.de/data/pbmc3k_raw.h5ad',
                     'filename': 'pbmc3k_raw.h5ad', 'expected_sha256': '89a96f1beaa2dd83a687666d3f19a4513ac27a2a2d12581fcd77afed7ea653a1',
                     'source': 'https://www.10xgenomics.com/datasets/3-k-pbm-cs-from-a-healthy-donor-1-standard-1-1-0',
                     'tissue': 'blood', 'reference_genome': 'hg19'},
    'mouse_brain_1k': {'species': 'mouse', 'url': 'https://cf.10xgenomics.com/samples/cell-exp/3.0.0/neuron_1k_v3/neuron_1k_v3_filtered_feature_bc_matrix.h5',
                        'filename': 'neuron_1k_v3_filtered_feature_bc_matrix.h5',
                        'source': 'https://www.10xgenomics.com/datasets/1-k-brain-cells-from-an-e-18-mouse-v-3-chemistry-3-standard-3-0-0',
                        'tissue': 'embryonic brain', 'reference_genome': 'mm10'},
}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('data/app-examples'))
    parser.add_argument('--manifest',type=Path,default=Path('examples/app-datasets.json'))
    parser.add_argument('--publish-bucket')
    args=parser.parse_args()
    import anndata as ad
    import scanpy as sc
    from pbmc_platform.app.adapter import inspect, selected
    from pbmc_platform.app.contracts import Configuration
    from pbmc_platform.app.blobs import GCSBlobs
    from importlib.metadata import version
    args.output.mkdir(parents=True,exist_ok=True)
    previous=json.loads(args.manifest.read_text(encoding='utf-8')) if args.manifest.exists() else {}
    results={}
    for key,spec in DATASETS.items():
        source=args.output/spec['filename']
        if not source.exists():
            temporary = source.with_suffix(source.suffix + '.download')
            with urlopen(Request(spec['url'], headers={'User-Agent': 'Mozilla/5.0'}),timeout=120) as response,temporary.open('wb') as destination:
                import shutil
                shutil.copyfileobj(response,destination)
            temporary.replace(source)
        checksum=hashlib.sha256(source.read_bytes()).hexdigest()
        expected=spec.get('expected_sha256') or previous.get(key,{}).get('source_sha256')
        if expected and checksum!=expected:raise ValueError('Source checksum mismatch: '+key)
        destination=args.output/(key+'.h5ad')
        if key=='human_pbmc3k':
            import shutil
            shutil.copyfile(source,destination)
        else:
            matrix=sc.read_10x_h5(source,gex_only=True)
            # Keep original feature IDs and symbols. No normalization, filtering or subsampling.
            matrix.write_h5ad(destination,compression='gzip')
        info=inspect(destination)
        config=Configuration(species=spec['species'],counts='X',gene_ids='gene_ids',gene_symbols='index',
                             mito_mode='prefix',mito_value='MT-' if spec['species']=='human' else 'mt-',
                             min_genes=200,max_genes=2500 if spec['species']=='human' else 10000,
                             max_mito_pct=5 if spec['species']=='human' else 20,tissue=spec['tissue'],reference_genome=spec['reference_genome'],
                             source=spec['source'],attribution='10x Genomics, CC BY 4.0',single_sample=True,public_data=True)
        checked=selected(destination,config)
        result={**spec,'source_sha256':checksum,'prepared_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                'prepared_filename':destination.name,'bytes':destination.stat().st_size,
                'cells':checked.n_obs,'features':checked.n_vars,'nnz':checked.X.nnz,'licence':'CC-BY-4.0',
                'conversion_runtime':{k:version(k) for k in ('scanpy','anndata','h5py')},
                'example_configuration':config.model_dump(),
                'policy_note':'Demonstration settings only; not a recommendation or a clinically validated policy.'}
        if args.publish_bucket:
            result['object']=GCSBlobs('pbmc-okf-portfolio',args.publish_bucket).write('examples/'+destination.name,destination.read_bytes())
        results[key]=result
    args.manifest.parent.mkdir(parents=True,exist_ok=True)
    args.manifest.write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:{x:v[x] for x in ('cells','features','nnz','bytes')} for k,v in results.items()},indent=2))


if __name__=='__main__':main()
