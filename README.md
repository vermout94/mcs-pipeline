# mcs-pipeline

Reference CI/CD pipeline and measurement harness for a master's thesis on the cost of software supply chain controls in GitHub Actions: SBOM generation and vulnerability scanning (Syft, Grype), artifact signing (cosign with Sigstore), build provenance (`actions/attest`) and policy gates (OPA, Conftest).

The experiment varies the four controls in a full 2^4 factorial design. It measures pipeline duration (p50, p95 and p99 per stage and in total), failure rates by cause and the false positive rate of the gates.

This repository is the replication package of the thesis. The thesis refers to tags (`campaign-YYYY-MM-DD`) and commit hashes of this repository.

Status: under construction. The repository contains the application under build. Pipeline, harness and measurement data follow.

## Application under build

`app/` contains [Spring PetClinic](https://github.com/spring-projects/spring-petclinic) at commit `500158f732419217507c7656904b8e6aa1bcc0d6`, licensed under the Apache License 2.0. The source code is copied without changes. [`app/UPSTREAM.md`](app/UPSTREAM.md) lists the origin of the copy, every deviation from upstream and the build parameters the pipeline uses.

## License

Licensed under the Apache License, Version 2.0, see [`LICENSE`](LICENSE). [`NOTICE`](NOTICE) names the copyright holder and the included third party work. Spring PetClinic keeps its license text in [`app/LICENSE.txt`](app/LICENSE.txt).
