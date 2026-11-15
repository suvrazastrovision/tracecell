# TraceCell documentation

[README](README.md) · [App guide](APP.md) · [Operations guide](OPERATIONS.md)

Start with the [quickstart guide](README.md#quickstart-guide) to run TraceCell
locally. Use the topics below for detailed instructions, implementation context
and recorded evidence. The upload app and the archived scheduled PBMC pipeline
are separate workflows.

## App setup and usage

| Topic | Documentation |
| --- | --- |
| Research scope, costs and planned work | [Scope and limitations](APP.md#scope-and-limitations) |
| Local startup and step-by-step use | [Run on your computer](APP.md#run-on-your-computer) |
| Port conflicts and browser origin | [Local port configuration](APP.md#local-port-configuration) |
| File format, annotations and resource limits | [Supported data and limits](APP.md#supported-data-and-limits) |
| Plots, downloads and same-sample QC comparisons | [Usage walkthrough](APP.md#run-on-your-computer) |
| Processing stages, elapsed time and estimates | [Processing progress](APP.md#processing-progress-and-time-estimates) |
| Optional local model settings | [Environment configuration](APP.md#optional-env-configuration) |
| Cited answers, paper uploads and confirmed QC rules | [Models and paper comparisons](APP.md#run-evidence-questions-and-research-paper-references) |
| API explorer and OpenAPI schema | [Swagger guide](APP.md#explore-the-backend-with-swagger) |
| Run recovery, deletion and cleanup | [Failure recovery and deletion](APP.md#failure-recovery-and-deletion) |

## Architecture and development

| Topic | Documentation |
| --- | --- |
| Technology stack and workflow design | [Architecture and tooling](OPERATIONS.md#architecture-and-tooling) |
| Upload app data flow and publication checks | [App data flow](APP.md#data-flow) |
| Scheduled PBMC pipeline, receipts and independent validation | [Complete data pipeline](OPERATIONS.md#complete-data-pipeline) |
| Dependency groups and historical environment snapshots | [Python dependencies](OPERATIONS.md#python-dependencies), [lock notes](locks/README.md) |
| Local checks and cross-platform automation | [Tests and CI](OPERATIONS.md#tests-and-ci) |
| Docker targets, authentication and recovery-test images | [Container builds](OPERATIONS.md#container-builds) |
| Diagram and badge generation | [Documentation assets](OPERATIONS.md#documentation-assets) |

## Cloud deployment and operations

| Topic | Documentation |
| --- | --- |
| Adapting the portfolio configuration to your project | [Deployment portability](OPERATIONS.md#deployment-portability) |
| App infrastructure, Firebase sign-in and launch checks | [GCP deployment and sign-in](APP.md#gcp-deployment-and-sign-in) |
| Scheduler, workflow locks and failure alerts | [Cloud pipeline management](OPERATIONS.md#how-the-cloud-pipeline-is-managed) |
| Running QC locally or triggering the provisioned cloud pipeline | [Run the processing](OPERATIONS.md#run-the-processing) |
| App failures, blocked slots and data deletion | [Recovery and deletion](APP.md#failure-recovery-and-deletion) |
| Archived assistant with a pinned example publication | [Archived assistant demo](OPERATIONS.md#archived-assistant-demo-powershell) |

## Evidence and release status

Recorded acceptance results describe a historical release snapshot. Consult the
[current CI workflow](.github/workflows/ci.yml) for checks applied to new changes;
cloud access and hosted launch requirements are documented separately.

| Record | Contents |
| --- | --- |
| [Example data and evidence](APP.md#example-data-and-evidence) | Human/mouse dimensions, example outcomes and local validation scope. |
| [Example catalog](examples/app-datasets.json) | Sources, checksums, mappings, attribution and preparation versions. |
| [Acceptance evidence](examples/app-acceptance.json) | Recorded workflow IDs, publication pins and report checksums. |
| [Checks and remaining launch gates](APP.md#checks) | Verification commands and hosted-access requirements. |
| [Historical implementation status](APP.md#accepted-implementation-status-historical-release-snapshot) | Accepted results and known limits of the recorded release. |
| [Verified PBMC report](examples/pbmc-verified-report.md) | Readable report for the archived PBMC result. |

## Licensing and attribution

- [Project licence](LICENSE): AGPL-3.0-only for original project code.
- [Third-party notices](THIRD_PARTY_NOTICES.md): bundled Swagger UI, public
  datasets, dependencies and user-supplied papers.
- [Release DOI](https://doi.org/10.5281/zenodo.24023343): archived project release.
