# mcs-pipeline

Reference CI/CD pipeline and measurement harness for a master's thesis on the cost of software supply chain controls in GitHub Actions: SBOM generation and vulnerability scanning (Syft, Grype), artifact signing (cosign with Sigstore), build provenance (`actions/attest`) and policy gates (OPA, Conftest).

The experiment varies the four controls in a full 2^4 factorial design. It measures pipeline duration (p50, p95 and p99 per stage and in total), failure rates by cause and the false positive rate of the gates.

Status: under construction. The repository contains no pipeline, harness or measurement data yet.
