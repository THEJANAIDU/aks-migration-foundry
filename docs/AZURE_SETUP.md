# Azure and Azure DevOps setup

## Two repository contracts

The application repository owns code, Dockerfile, the existing VM release pipeline, and the new container-image pipeline. The infrastructure repository owns AKS/ACR infrastructure, Helm deployment configuration, protected namespaces and the new artifact-driven release pipeline. They may be GitHub or Azure Repos repositories; the CLI uses ordinary Git origins and existing authentication.

In Azure DevOps, import each generated YAML as a **new pipeline** in its owning repo. The original VM pipeline is not edited or disabled. Authorize only the intended pipeline to use each repository, service connection, agent pool and Environment. For cross-project pipeline resources, grant the consuming project's build identity access to the source pipeline artifacts; review project-scoped job authorization settings. Do not broadly disable authorization protections.

Set the application pipeline source name/project and allowed branch in config. The release resource uses a completion trigger for a successful Package stage on that branch and downloads the artifact from that exact run. Set each pipeline's default branch for manual/scheduled builds to its reviewed default branch, with the `refs/heads/` prefix where required for cross-project triggers. Merge the release YAML into the infrastructure default branch before expecting completion triggers to work.

## Identity separation

| Connection | Required capability | Not required |
|---|---|---|
| Build WIF connection | ACR image push/read for the selected registry | AKS or VM administrator |
| Deploy WIF connection | Obtain non-admin AKS user credentials; deploy Helm resources in only the two namespaces; read namespace labels | VM write, subscription Owner |
| Platform WIF connection | Reviewed resource provisioning and role assignments at the resource/network scopes; bootstrap namespace policy | Application credentials or VM guest access |

For the supplied ACR **LegacyRegistryPermissions** mode, scope AcrPush to the build principal and AcrPull to the kubelet identity. For an existing ABAC-enabled registry, use the appropriate repository reader/writer roles instead: legacy AcrPull/AcrPush do not grant equivalent ABAC data access. Do not change an existing registry's authorization mode without review.

For AKS Azure RBAC, grant the deployment principal Cluster User for obtaining credentials and the appropriate namespace-scoped Kubernetes RBAC Writer permissions. Helm needs its release Secrets and chart resources. The namespace-label preflight also requires namespace metadata read; a platform administrator must grant that narrowly where necessary. The pipeline intentionally fails instead of using `--admin` or disabling local-account protections.

A platform identity that assigns roles needs appropriately scoped role-assignment permissions. Prefer separated planning/apply identities and approved deployment policies for regulated environments. Use workload identity federation service connections according to your organization's current Entra issuer configuration; do not copy long-lived client secrets into YAML.

## Private agents and platform prerequisites

The agent pool named in config must have Python 3, Docker, Azure CLI, Helm 3, kubectl, kubelogin and Trivy, with reviewed versions installed by the platform team. Build agents need access to dependency registries, the vulnerability database and ACR. Release agents need private AKS API connectivity, ACR/DNS access and Azure identity endpoints. Keep untrusted pull-request builds away from privileged shared production agents.

The optional Bicep reference creates a private AKS control plane, OIDC/workload identity capability, Azure RBAC, three zonal Linux system nodes, managed control/kubelet identities, private Premium ACR and an ACR private endpoint/DNS zone. It uses an **existing** landing-zone VNet, AKS subnet and private-endpoint subnet. It is not a landing-zone replacement.

Review all generated infrastructure parameters: regional Kubernetes support and VM quota/zones, non-overlapping pod/service/VNet/on-premises ranges, outbound connectivity, private DNS forwarding/links and existing centralized DNS zones. A private DNS zone with an existing central equivalent must be integrated rather than blindly duplicated. AKS automatic private DNS also needs connectivity from the agent VNet. Set the correct resource group and matching cluster/registry names in both application config and infrastructure parameters.

The reference uses managed load-balancer egress, not a custom enterprise firewall. Organizations requiring user-defined routing, centralized Azure Firewall or custom private DNS must adapt and validate it. The platform pipeline refuses placeholder parameters, runs what-if, requests Ops approval, then applies. This can incur Azure costs; no infrastructure is provisioned by the local preparation command.

## Production approval is Ops-owned

Create the staging and production Azure DevOps Environments before first release. On production, configure **Approvals and checks** with the real Ops/DevOps engineer group, disallow approver self-approval, and restrict check administration. Add branch controls, an exclusive deployment lock and a business-hours/change-management check if required. Set the same group in `pipeline.ops_approvers` for the YAML ManualValidation task. A missing/invalid group must be fixed before deploying; sample group names are not real permissions.

The production deployment depends on both staging and Ops approval. A newer artifact cannot alter that run's image. Pending older releases still need an operational policy: reject superseded approvals rather than approving stale runs. Environment checks are managed outside YAML so repository contributors cannot simply delete the production approval policy.

## Workload identity and secrets

A nonempty workload identity client ID adds the service-account annotation and pod label only. A platform administrator still must create the identity, federated credential for each exact namespace/service account, and narrowly scoped data-plane roles. The application must support Azure SDK workload credentials. The generated pod does not mount a default Kubernetes API token; the Azure workload-identity webhook projects its own token when configured.

Never put secrets into config, Helm values, Docker build arguments or Pipeline Artifacts. Integrate your approved Key Vault/CSI or external-secret solution and test refresh/rotation. Default-deny application egress permits DNS and reviewed CIDR/port rules only; plan access to identity, databases, storage and telemetry. Azure service addresses can change: enterprise FQDN egress usually needs an approved firewall/Cilium policy design rather than hard-coded guessed IPs. The chart assumes network-policy enforcement and kube-dns labels; adjust for NodeLocal DNS or other DNS designs.

## Primary references

- [Azure Pipelines pipeline-completion triggers](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/pipeline-triggers?view=azure-devops)
- [ManualValidation task](https://learn.microsoft.com/en-us/azure/devops/pipelines/tasks/reference/manual-validation-v1?view=azure-pipelines)
- [Environment approvals](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals?view=azure-devops)
- [AKS workload identity](https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview)
- [ACR ABAC permissions](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-rbac-abac-repository-permissions)
