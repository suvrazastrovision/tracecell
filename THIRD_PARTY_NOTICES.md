# Third-party notices

The root [LICENSE](LICENSE) applies to original TraceCell project code under
**AGPL-3.0-only**. Third-party code, data and publications retain their own terms.

## Swagger UI

The bundled `swagger-ui-dist` 5.34.1 assets are licensed under Apache-2.0.
Their [upstream license](src/pbmc_platform/app/static/swagger/LICENSE) is retained.
The [asset manifest](src/pbmc_platform/app/static/swagger/version.json) records
the npm source, package integrity and original file hashes. The project license
does not replace this license or these notices.

## Public example datasets

The human PBMC3k and mouse E18 brain examples are from 10x Genomics and are
attributed under **CC BY 4.0**. See the [example catalog](examples/app-datasets.json)
for original dataset links, checksums, dimensions and preparation details.
The mouse matrix is converted to `.h5ad` without normalization or cell subsampling.
These data are not relicensed under the project's software license.

The data sources are:

- [3k PBMCs from a healthy donor](https://www.10xgenomics.com/datasets/3-k-pbm-cs-from-a-healthy-donor-1-standard-1-1-0).
- [1k brain cells from an E18 mouse](https://www.10xgenomics.com/datasets/1-k-brain-cells-from-an-e-18-mouse-v-3-chemistry-3-0-0).
- [Creative Commons Attribution 4.0 terms](https://creativecommons.org/licenses/by/4.0/).

## Dependencies and user-supplied papers

Installed dependencies and container parent images retain their respective
licenses. Dependency declarations are in [pyproject.toml](pyproject.toml);
the root license does not relicense these packages or private container images.
Research papers supplied by users remain subject to their publishers' or
authors' terms; uploading a paper does not change its copyright or license.
