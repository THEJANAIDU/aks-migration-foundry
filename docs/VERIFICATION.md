# Verification and limits

Verified locally on macOS with Python 3.9, Git, Docker and Helm 3.19.0:

- 13-test suite passed, including real successful and failed container runtime checks, branch gating, original VM pipeline preservation, separate infrastructure generation, artifact validation, Ops approval structure, and rendered Helm security policies.
- An additional Docker-unavailable test passed: it writes a blocked report rather than creating an application branch.
- AKS/ACR Bicep compiled using Azure CLI/Bicep, without provisioning Azure resources.
- GitHub Actions repeats tests with real Docker builds and Helm rendering, then compiles Bicep.

Not verified against a live customer Azure tenant: infrastructure what-if/apply, private DNS/routing, federated service connections, Azure DevOps pipeline completion triggers and group resolution, vulnerability scanner execution, AKS rollout/rollback, workload identity, network-policy enforcement, application data migration or production traffic cutover.

The supported adapter requires a committed Linux Dockerfile and HTTP health endpoint. It does not automatically rewrite arbitrary VM applications. Unknown packaging, Windows, stateful/local-disk dependencies and unresolved reviews are blocked for the application team. Runtime health checks prove only local startup under the tested restrictions; they do not establish business correctness, scalability, HA or regulatory compliance.

Production requires customer-specific assessment, identity/network validation, data and rollback plans, staged business/load tests, and Ops-owned Environment approvals. The generated assets are a production-oriented baseline, not an unconditional production-readiness certification. No VM is shut down, no data is copied, and no traffic changes automatically.
