#!/usr/bin/env python3
"""Deterministic, non-destructive VM-to-AKS migration preparation agent."""
import argparse
import hashlib
import os
import tarfile
import tempfile
import time
from uuid import uuid4
from urllib.request import urlopen
import ipaddress
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "0.1.0"
RISKS = ("local_disk", "database_in_vm", "in_memory_sessions", "windows_required", "privileged_access", "background_jobs")

class MigrationError(Exception):
    pass

def command(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise MigrationError("Command failed: " + args[0] + " " + args[1] + "\n" + result.stderr.strip())
    return result.stdout.strip()

def git(repo, *args):
    return command(["git", "-C", str(repo), *args])

def require(condition, message):
    if not condition:
        raise MigrationError(message)

def identifier(value, pattern, label):
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, "Invalid " + label)
    return value

def read_config(path):
    try:
        cfg = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise MigrationError("Cannot read configuration: " + str(exc))
    require(isinstance(cfg, dict), "Configuration must be an object")
    allowed = {"version", "app", "source", "azure", "pipeline", "assessment", "network", "workload_identity_client_id"}
    require(set(cfg) == allowed, "Configuration keys must be exactly: " + ", ".join(sorted(allowed)))
    require(cfg["version"] == 1, "Only configuration version 1 is supported")
    for key in ("app", "source", "azure", "pipeline", "assessment", "network"):
        require(isinstance(cfg[key], dict), key + " must be an object")
    def keys(section, expected):
        require(set(cfg[section]) == set(expected), "Unexpected or missing fields in " + section)
    keys("app", ("name", "port", "health_path"))
    keys("source", ("dockerfile", "context", "vm_pipeline"))
    keys("azure", ("subscription", "resource_group", "aks_name", "acr_name"))
    keys("pipeline", ("service_connection", "build_service_connection", "platform_service_connection", "agent_pool", "staging_environment", "production_environment", "application_pipeline", "application_project", "application_branch", "ops_approvers"))
    keys("assessment", RISKS + ("dependencies_reviewed", "health_checked", "container_tested", "rollback_reviewed"))
    keys("network", ("ingress_namespace", "egress"))
    identifier(cfg["app"]["name"], r"[a-z][a-z0-9-]{0,29}", "app name")
    port = cfg["app"]["port"]
    require(type(port) is int and 1024 <= port <= 65535, "Application port must be an unprivileged port between 1024 and 65535")
    identifier(cfg["app"]["health_path"], r"/[A-Za-z0-9/_-]*", "health path")
    for key in ("dockerfile", "context", "vm_pipeline"):
        value = cfg["source"][key]
        identifier(value, r"[A-Za-z0-9_.\-/]+", "source " + key)
        require(not Path(value).is_absolute() and ".." not in Path(value).parts, "Source paths must stay inside the repository")
    identifier(cfg["azure"]["subscription"], r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}", "subscription ID")
    for key in ("resource_group", "aks_name"):
        identifier(cfg["azure"][key], r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", "Azure " + key)
    identifier(cfg["azure"]["acr_name"], r"[a-z][a-z0-9]{4,49}", "ACR name")
    for key, value in cfg["pipeline"].items():
        pattern = r"[A-Za-z0-9][A-Za-z0-9_. /-]{0,119}"
        if key == "ops_approvers": pattern = r"[A-Za-z0-9\[\]_. @\\-]{1,120}"
        identifier(value, pattern, "pipeline " + key)
    identifier(cfg["pipeline"]["application_branch"], r"[A-Za-z0-9][A-Za-z0-9/_-]{0,100}", "application branch")
    require(cfg["pipeline"]["staging_environment"] != cfg["pipeline"]["production_environment"], "Staging and production environments must differ")
    for value in cfg["assessment"].values():
        require(value is None or type(value) is bool, "Assessment answers must be true, false or null")
    identifier(cfg["network"]["ingress_namespace"], r"[a-z][a-z0-9-]{0,62}", "ingress namespace")
    require(isinstance(cfg["network"]["egress"], list), "Egress rules must be a list")
    for rule in cfg["network"]["egress"]:
        require(isinstance(rule, dict) and set(rule) == {"cidr", "port"}, "Egress rule requires cidr and port only")
        try:
            ipaddress.ip_network(rule["cidr"], strict=True)
        except (ValueError, TypeError):
            raise MigrationError("Invalid egress CIDR")
        require(type(rule["port"]) is int and 1 <= rule["port"] <= 65535, "Invalid egress port")
    client = cfg["workload_identity_client_id"]
    require(client == "" or (isinstance(client, str) and re.fullmatch(r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}", client)), "Invalid workload identity client ID")
    return cfg

def blockers(cfg):
    issues = []
    for key in RISKS:
        value = cfg["assessment"][key]
        if value is None:
            issues.append(key + ": discovery/review is incomplete")
        elif value:
            issues.append(key + ": outside the stateless Linux HTTP adapter; remediate and reassess")
    for key in ("dependencies_reviewed", "health_checked", "rollback_reviewed"):
        if cfg["assessment"][key] is not True:
            issues.append(key + ": evidence and owner review required")
    return issues

def source_check(repo, cfg, ref="HEAD"):
    require(git(repo, "rev-parse", "--show-toplevel") == str(repo.resolve()), "--repo must point to the repository root")
    tracked = set(git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())
    for name in (cfg["source"]["dockerfile"], cfg["source"]["vm_pipeline"]):
        require(name in tracked, "Required source file not present at base revision: " + name)
        mode = git(repo, "ls-tree", ref, "--", name).split()[0]
        require(mode in ("100644", "100755"), "Source files must not be symlinks or submodules: " + name)
    context = cfg["source"]["context"]
    require(context == "." or any(name.startswith(context.rstrip("/") + "/") for name in tracked), "Build context not found at base revision")
    dockerfile = git(repo, "show", ref + ":" + cfg["source"]["dockerfile"])
    require(re.search(r"(?im)^FROM\s+", dockerfile) is not None, "Dockerfile has no FROM instruction")
    require(not re.search(r"(?im)^FROM\s+.*(?:windows|nanoserver|servercore)", dockerfile), "Windows containers need a different adapter and Windows node pool")
    return tracked

def report(cfg, revision):
    issues = blockers(cfg)
    return {"agent_version": VERSION, "application": cfg["app"]["name"], "source_revision": revision,
            "status": "blocked" if issues else "ready-for-staging-review", "blockers": issues,
            "not_performed": ["VM filesystem copying", "database migration", "Azure provisioning", "AKS deployment", "DNS/traffic cutover", "VM shutdown"],
            "deployment_model": "Azure DevOps + Helm; immutable ACR digest; staging then human-gated production"}

def values(cfg, environment):
    return {"appName": cfg["app"]["name"], "image": {"repository": cfg["azure"]["acr_name"] + ".azurecr.io/" + cfg["app"]["name"], "digest": ""},
            "port": cfg["app"]["port"], "healthPath": cfg["app"]["health_path"], "replicas": 2,
            "environment": environment, "workloadIdentityClientId": cfg["workload_identity_client_id"],
            "ingressNamespace": cfg["network"]["ingress_namespace"], "egress": cfg["network"]["egress"],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1000m", "memory": "512Mi"}},
            "autoscaling": {"minReplicas": 2, "maxReplicas": 6, "cpuPercent": 70}}


def azure_step(cfg, connection, script, display, indent=10, name=None):
    space = " " * indent
    lines = [space + "- task: AzureCLI@2", space + "  displayName: " + display]
    if name: lines.append(space + "  name: " + name)
    lines += [space + "  inputs:", space + "    azureSubscription: '" + cfg["pipeline"][connection] + "'", space + "    scriptType: bash", space + "    scriptLocation: inlineScript", space + "    inlineScript: |"]
    lines += [space + "      " + line for line in script.strip().splitlines()]
    return "\n".join(lines)


def approval_job(cfg):
    return """      - job: OpsApproval
        pool: server
        timeoutInMinutes: 1440
        steps:
          - task: ManualValidation@1
            timeoutInMinutes: 1430
            inputs:
              notifyUsers: ''
              approvers: '@@OPS@@'
              allowApproversToApproveTheirOwnRuns: false
              instructions: 'Ops/DevOps: review staging evidence, pinned image digest, platform readiness, data and rollback plans. No VM traffic is switched by this deployment.'
              onTimeout: reject
""".replace("@@OPS@@", cfg["pipeline"]["ops_approvers"])


def render_application(cfg):
    app, a, p = cfg["app"]["name"], cfg["azure"], cfg["pipeline"]
    script = """set -euo pipefail
for tool in python3 docker trivy; do command -v "$tool"; done
az account set --subscription '@@SUB@@'
export DOCKER_CONFIG="$(Agent.TempDirectory)/docker-$(Build.BuildId)"
mkdir -p "$DOCKER_CONFIG"
chmod 700 "$DOCKER_CONFIG"
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
az acr login --name '@@ACR@@'
image='@@ACR@@.azurecr.io/@@APP@@:$(Build.BuildId)'
report="$(Agent.TempDirectory)/container-report.json"
python3 .aks-migration/agent.py containerize --repo . --config .aks-migration/config.json --image "$image" --report "$report"
trivy image --exit-code 1 --severity HIGH,CRITICAL "$image"
docker push "$image"
digest="$(az acr repository show --name '@@ACR@@' --image '@@APP@@:$(Build.BuildId)' --query digest --output tsv)"
python3 .aks-migration/agent.py make-artifact --config .aks-migration/config.json --report "$report" --digest "$digest" --build-id '$(Build.BuildId)' --output '$(Build.ArtifactStagingDirectory)/aks-image/image.json'
"""
    for key,value in (("SUB",a["subscription"]),("ACR",a["acr_name"]),("APP",app)):script=script.replace("@@"+key+"@@",value)
    return """# NEW application CI pipeline; existing VM release YAML is preserved.
trigger:
  branches:
    include: ['@@BRANCH@@']
pr: none
pool:
  name: '@@POOL@@'
stages:
  - stage: Package
    jobs:
      - job: Build
        steps:
          - checkout: self
@@BUILD@@
          - task: PublishPipelineArtifact@1
            inputs:
              targetPath: '$(Build.ArtifactStagingDirectory)/aks-image'
              artifact: aks-image
""".replace('@@BRANCH@@',p['application_branch']).replace('@@POOL@@',p['agent_pool']).replace('@@BUILD@@',azure_step(cfg,'build_service_connection',script,'Containerize, health-test, scan and publish immutable image'))


def render_release(cfg):
    app,a,p=cfg['app']['name'],cfg['azure'],cfg['pipeline']
    header="""# INFRA repository: automatic successful application artifact -> staging -> Ops approval -> production.
trigger: none
pr: none
pool:
  name: '@@POOL@@'
resources:
  pipelines:
    - pipeline: application
      project: '@@PROJECT@@'
      source: '@@PIPELINE@@'
      branch: 'refs/heads/@@BRANCH@@'
      trigger:
        branches:
          include: ['refs/heads/@@BRANCH@@']
        stages: [Package]
stages:
  - stage: ValidateArtifact
    jobs:
      - job: Evidence
        steps:
          - checkout: self
          - download: application
            artifact: aks-image
          - bash: |
              set -euo pipefail
              python3 .aks-platform/agent.py verify-artifact --config .aks-platform/config.json --artifact '$(Pipeline.Workspace)/application/aks-image/image.json' --expected-source-revision '$(resources.pipeline.application.sourceCommit)' --expected-build-id '$(resources.pipeline.application.runID)' --azure-output
            name: pin
            displayName: Validate the exact triggering run artifact
"""
    for key,value in (('POOL',p['agent_pool']),('PROJECT',p['application_project']),('PIPELINE',p['application_pipeline']),('BRANCH',p['application_branch'])):header=header.replace('@@'+key+'@@',value)
    stages=[]
    for stage in ('staging','production'):
        namespace=app+'-'+stage
        script="""set -euo pipefail
for tool in helm kubectl kubelogin; do command -v "$tool"; done
az account set --subscription '@@SUB@@'
export KUBECONFIG="$(Agent.TempDirectory)/aks-@@STAGE@@-$(Build.BuildId).config"
trap 'rm -f "$KUBECONFIG"' EXIT
az aks get-credentials --resource-group '@@RG@@' --name '@@AKS@@' --file "$KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig -l azurecli
kubectl get namespace '@@NS@@' -o json | python3 -c 'import json,sys; assert json.load(sys.stdin)["metadata"]["labels"].get("pod-security.kubernetes.io/enforce") == "restricted", "Namespace must enforce restricted Pod Security"'
previous="$(helm status '@@APP@@' --namespace '@@NS@@' --output json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')" || previous=""
helm upgrade --install '@@APP@@'  .aks-platform/chart --namespace '@@NS@@' --values '.aks-platform/values-@@STAGE@@.json' --set-string image.digest="$(imageDigest)" --atomic --wait --timeout 10m
kubectl rollout status deployment/@@APP@@ --namespace '@@NS@@' --timeout=300s
if ! helm test '@@APP@@' --namespace '@@NS@@' --logs --timeout 3m; then
  if [ -n "$previous" ]; then
    helm rollback '@@APP@@' "$previous" --namespace '@@NS@@' --wait --timeout 10m
  else
    helm uninstall '@@APP@@' --namespace '@@NS@@' --wait
  fi
  exit 1
fi
"""
        for key,value in (('SUB',a['subscription']),('RG',a['resource_group']),('AKS',a['aks_name']),('APP',app),('STAGE',stage),('NS',namespace)):script=script.replace('@@'+key+'@@',value)
        production=stage=='production'
        stages.append('  - stage: '+stage.title()+'\n    dependsOn: ['+('ValidateArtifact, Staging' if production else 'ValidateArtifact')+']\n    variables:\n      imageDigest: $[ stageDependencies.ValidateArtifact.Evidence.outputs[\'pin.imageDigest\'] ]\n    jobs:\n'+(approval_job(cfg) if production else '')+'      - deployment: Deploy'+stage.title()+'\n'+('        dependsOn: OpsApproval\n' if production else '')+"        environment: '"+p[stage+'_environment']+"'\n        strategy:\n          runOnce:\n            deploy:\n              steps:\n                - checkout: self\n"+azure_step(cfg,'service_connection',script,'Deploy the pinned digest and run smoke tests',indent=16)+'\n')
    return header+'\n'.join(stages)


def render_platform(cfg):
    a,p=cfg['azure'],cfg['pipeline']
    validate="""set -euo pipefail
if grep -q REPLACE .aks-platform/infra/parameters.example.json; then echo 'Complete and review infrastructure parameters first'; exit 1; fi
az account set --subscription '@@SUB@@'
az deployment group what-if --resource-group '@@RG@@' --template-file .aks-platform/infra/main.bicep --parameters @.aks-platform/infra/parameters.example.json
""".replace('@@SUB@@',a['subscription']).replace('@@RG@@',a['resource_group'])
    apply="""set -euo pipefail
az account set --subscription '@@SUB@@'
az deployment group create --resource-group '@@RG@@' --template-file .aks-platform/infra/main.bicep --parameters @.aks-platform/infra/parameters.example.json
kubectl_config="$(Agent.TempDirectory)/aks-platform-$(Build.BuildId).config"
trap 'rm -f "$kubectl_config"' EXIT
az aks get-credentials --resource-group '@@RG@@' --name '@@AKS@@' --file "$kubectl_config" --overwrite-existing
export KUBECONFIG="$kubectl_config"
kubelogin convert-kubeconfig -l azurecli
kubectl apply -f .aks-platform/namespaces.yaml
""".replace('@@SUB@@',a['subscription']).replace('@@RG@@',a['resource_group']).replace('@@AKS@@',a['aks_name'])
    return "trigger: none\npr: none\npool:\n  name: '"+p['agent_pool']+"'\nstages:\n  - stage: Plan\n    jobs:\n      - job: WhatIf\n        steps:\n          - checkout: self\n"+azure_step(cfg,'platform_service_connection',validate,'Review Azure resource changes')+'\n  - stage: Apply\n    dependsOn: Plan\n    jobs:\n'+approval_job(cfg)+"      - deployment: ApplyPlatform\n        dependsOn: OpsApproval\n        environment: '"+p['production_environment']+"'\n        strategy:\n          runOnce:\n            deploy:\n              steps:\n                - checkout: self\n"+azure_step(cfg,'platform_service_connection',apply,'Apply reviewed infrastructure and protected namespaces',indent=16)+'\n'


def namespaces(cfg):
    objects=[]
    for env in ('staging','production'):
        name=cfg['app']['name']+'-'+env
        objects.append({'apiVersion':'v1','kind':'Namespace','metadata':{'name':name,'labels':{'pod-security.kubernetes.io/enforce':'restricted','pod-security.kubernetes.io/audit':'restricted','pod-security.kubernetes.io/warn':'restricted'}}})
        objects.append({'apiVersion':'v1','kind':'ResourceQuota','metadata':{'name':'workload-budget','namespace':name},'spec':{'hard':{'requests.cpu':'4','requests.memory':'4Gi','limits.cpu':'8','limits.memory':'8Gi','pods':'20'}}})
    return json.dumps({'apiVersion':'v1','kind':'List','items':objects},indent=2)+'\n'


def save_report(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Report contains no source contents, secrets or application logs.
    path.write_text(json.dumps(result, indent=2) + "\n")
    path.chmod(0o600)


def containerize(repo, cfg, revision, report_path, image=None, timeout=60):
    require(repo.resolve() not in Path(report_path).resolve().parents, "Report/build log must be outside the application repository")
    result = report(cfg, revision)
    result["containerization"] = "blocked"
    image = image or "aks-foundry-check:" + uuid4().hex
    identifier(image, r"[A-Za-z0-9][A-Za-z0-9._:/-]+", "image reference")
    result["image"] = image
    container = None
    transient = image.startswith("aks-foundry-check:")
    try:
        require(not result["blockers"], "Owner readiness review is incomplete: " + "; ".join(result["blockers"]))
        source_check(repo, cfg, revision)
        command(["docker", "info", "--format", "{{.ServerVersion}}"])
        with tempfile.TemporaryDirectory(prefix="aks-containerize-") as temporary:
            context_root = Path(temporary)
            archive = context_root / "source.tar"
            with archive.open("wb") as stream:
                subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", revision], stdout=stream, check=True)
            source = context_root / "source"
            source.mkdir()
            with tarfile.open(archive) as bundle:
                members = bundle.getmembers()
                for member in members:
                    require(not Path(member.name).is_absolute() and ".." not in Path(member.name).parts and (member.isfile() or member.isdir()), "Build export rejects symlinks, submodules and unsafe archive entries")
                bundle.extractall(source, members=members)
            logfile = Path(str(report_path) + ".build.log")
            logfile.parent.mkdir(parents=True, exist_ok=True)
            with logfile.open("w") as output:
                logfile.chmod(0o600)
                built = subprocess.run(["docker", "build", "--file", str(source / cfg["source"]["dockerfile"]), "--tag", image, str(source / cfg["source"]["context"])], stdout=output, stderr=subprocess.STDOUT)
            require(built.returncode == 0, "Container build failed; inspect the private local build log")
            container = command(["docker", "run", "--detach", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "512m", "--cpus", "1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=67108864", "--publish", "127.0.0.1::" + str(cfg["app"]["port"]), image])
            address = command(["docker", "port", container, str(cfg["app"]["port"]) + "/tcp"]).splitlines()[0]
            require(address.startswith("127.0.0.1:"), "Container health endpoint must be loopback-only")
            endpoint = "http://" + address + cfg["app"]["health_path"]
            deadline = time.monotonic() + timeout
            healthy = 0
            while time.monotonic() < deadline:
                try:
                    with urlopen(endpoint, timeout=2) as response:
                        healthy = healthy + 1 if response.status == 200 else 0
                    if healthy >= 2: break
                except OSError:
                    healthy = 0
                time.sleep(1)
            require(healthy >= 2, "Container did not pass two runtime health checks under non-root/read-only restrictions")
            result["containerization"] = "passed"
            result["status"] = "ready-for-staging-review"
            result["local_image_id"] = command(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
            result["runtime_policy"] = {"uid": 10001, "read_only": True, "capabilities_dropped": "ALL", "health_checks": 2}
    except (MigrationError, OSError, subprocess.SubprocessError) as exc:
        result["status"] = "blocked"
        result["blockers"].append(str(exc))
    finally:
        if container:
            subprocess.run(["docker", "rm", "--force", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if transient and shutil.which("docker"):
            subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        save_report(report_path, result)
    return result


def resolve_base(repo, explicit, refresh=False):
    require(git(repo, "rev-parse", "--show-toplevel") == str(repo.resolve()), "Repository root required")
    require(not git(repo, "status", "--porcelain"), "Source checkout must be clean")
    require(git(repo, "config", "user.name") and git(repo, "config", "user.email"), "Configure Git author name and email first")
    if refresh: git(repo, "fetch", "origin")
    base = explicit
    if not base:
        try: base = git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
        except MigrationError:
            candidates = [name for name in ("main", "master") if subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/" + name]).returncode == 0]
            require(len(candidates) == 1, "Cannot infer base; pass --base origin/master or origin/main")
            base = candidates[0]
    require(not base.startswith("-"), "Invalid base revision")
    return git(repo, "rev-parse", "--verify", base + "^{commit}")


def branch_preflight(repo, cfg, args, role):
    branch = args.branch or "migration/aks-" + cfg["app"]["name"] + ("-infra" if role == "infra" else "")
    require(not branch.startswith("-"), "Invalid branch")
    git(repo, "check-ref-format", "--branch", branch)
    require(subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/" + branch]).returncode != 0, "Migration branch already exists; choose another --branch")
    suffix = "-aks-platform-" if role == "infra" else "-aks-"
    target = Path(args.worktree).resolve() if args.worktree else repo.parent / (repo.name + suffix + cfg["app"]["name"])
    require(not target.exists() and target != repo and repo not in target.parents, "Worktree must be a new directory outside the source repository")
    return branch, target


def write_common(target, cfg, role):
    assets = target / (".aks-platform" if role == "infra" else ".aks-migration")
    require(not assets.exists(), "Migration assets already exist")
    assets.mkdir()
    shutil.copy2(ROOT / "aks_migrate.py", assets / "agent.py")
    (assets / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    for name in ("RUNBOOK.md", "AZURE_SETUP.md", "CUTOVER.md"):
        shutil.copy2(ROOT / "docs" / name, assets / name)
    return assets


def prepare_role(args, role):
    repo = Path(args.repo).resolve()
    cfg = read_config(args.config)
    revision = resolve_base(repo, args.base, args.refresh)
    tracked = set(git(repo, "ls-tree", "-r", "--name-only", revision).splitlines())
    prefix = ".aks-platform/" if role == "infra" else ".aks-migration/"
    paths = [".azure-pipelines/aks-platform.yml", ".azure-pipelines/aks-release.yml"] if role == "infra" else [".azure-pipelines/aks-image.yml"]
    require(not any(name.startswith(prefix) or name in paths for name in tracked), "Base contains migration assets; refusing overwrite")
    branch, target = branch_preflight(repo, cfg, args, role)
    result = None
    if role == "app":
        report_path = Path(args.report) if args.report else repo.parent / (repo.name + "-aks-assessment.json")
        result = containerize(repo, cfg, revision, report_path, timeout=args.timeout)
        if result["containerization"] != "passed":
            print(json.dumps({"application": "blocked", "branch_created": False, "report": str(report_path)}, indent=2))
            return 2
        cfg["assessment"]["container_tested"] = True
    # No application branch/worktree is created above this line.
    git(repo, "worktree", "add", "-b", branch, str(target), revision)
    try:
        assets = write_common(target, cfg, role)
        pipeline = target / ".azure-pipelines"
        pipeline.mkdir(exist_ok=True)
        if role == "infra":
            (assets / "namespaces.yaml").write_text(namespaces(cfg))
            shutil.copytree(ROOT / "templates/chart", assets / "chart")
            shutil.copytree(ROOT / "templates/infra", assets / "infra")
            for env in ("staging", "production"):
                (assets / ("values-" + env + ".json")).write_text(json.dumps(values(cfg, env), indent=2) + "\n")
            (pipeline / "aks-platform.yml").write_text(render_platform(cfg))
            (pipeline / "aks-release.yml").write_text(render_release(cfg))
            state = {"infrastructure": {"status": "assets-prepared", "cloud_applied": False, "live_validation": "pending"}, "application": {"status": "blocked-until-validated-artifact", "independent_repository": True}}
        else:
            (assets / "assessment.json").write_text(json.dumps(result, indent=2) + "\n")
            (pipeline / "aks-image.yml").write_text(render_application(cfg))
            state = {"application": {"status": "containerization-passed", "azure_ci_validation": "pending"}, "infrastructure": {"status": "separate-repository"}}
        (assets / "checklists.json").write_text(json.dumps(state, indent=2) + "\n")
        git(target, "add", assets.name, *paths)
        git(target, "commit", "-m", "Prepare AKS " + role + " migration assets for " + cfg["app"]["name"])
    except Exception:
        print("Worktree preserved for diagnosis: " + str(target), file=sys.stderr)
        raise
    if args.push: git(target, "push", "--set-upstream", "origin", branch)
    print(json.dumps({"role": role, "branch": branch, "worktree": str(target), "base_revision": revision, "pushed": args.push, "azure_changed": False}, indent=2))
    return 0


def inventory(args):
    identifier(args.subscription, r"[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12}", "subscription")
    identifier(args.resource_group, r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", "resource group")
    path = Path(args.output)
    require(not path.exists(), "Inventory destination already exists")
    data = command(["az", "vm", "list", "--subscription", args.subscription, "--resource-group", args.resource_group, "--query", "[].{id:id,name:name,os:storageProfile.osDisk.osType,size:hardwareProfile.vmSize}", "--output", "json", "--only-show-errors"])
    parsed = json.loads(data)
    require(isinstance(parsed, list), "Unexpected Azure inventory response")
    path.write_text(json.dumps({"vms": parsed, "limitations": "Metadata only; no VM commands, disks, application configs or secrets are accessed"}, indent=2) + "\n")
    path.chmod(0o600)
    print("Inventory saved locally; application dependency discovery still requires owner review")

def demo(args):
    parent = Path(args.destination).resolve()
    require(not parent.exists(), "Demo destination already exists")
    parent.mkdir(parents=True)
    app = parent / "application"
    infra = parent / "infrastructure"
    shutil.copytree(ROOT / "examples/sample-service", app)
    infra.mkdir()
    (infra / "README.md").write_text("Existing infrastructure repository; migration changes go to a separate branch.\n")
    for repo in (app, infra):
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "AKS Migration Foundry Demo")
        git(repo, "config", "user.email", "demo@localhost")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "Existing customer repository demo")
    args.config = str(ROOT / "examples/migration.demo.json")
    args.base, args.branch, args.worktree, args.refresh, args.push, args.report, args.timeout = "main", None, None, False, False, str(parent / "application-assessment.json"), 60
    args.repo = str(infra)
    prepare_role(args, "infra")
    args.repo = str(app)
    return prepare_role(args, "app")


def validate_artifact(cfg, data):
    expected = cfg["azure"]["acr_name"] + ".azurecr.io/" + cfg["app"]["name"]
    require(isinstance(data, dict) and set(data) == {"schema_version", "repository", "digest", "source_revision", "containerization", "build_id"}, "Invalid image artifact schema")
    require(data["schema_version"] == 1 and data["repository"] == expected, "Artifact targets an unexpected image repository")
    identifier(data["digest"], r"sha256:[a-f0-9]{64}", "artifact image digest")
    identifier(data["source_revision"], r"[a-f0-9]{40}|[a-f0-9]{64}", "artifact source revision")
    identifier(data["build_id"], r"[0-9]+", "artifact build ID")
    require(data["containerization"] == "passed", "Artifact has not passed containerization")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("prepare", help="Prepare infrastructure independently; create application branch only after containerization passes")
    p.add_argument("--app-repo", required=True)
    p.add_argument("--infra-repo", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--app-base")
    p.add_argument("--infra-base")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--push", action="store_true")
    p.add_argument("--report")
    p.add_argument("--timeout", type=int, default=60)
    for operation in ("prepare-app", "prepare-infra"):
        p = sub.add_parser(operation)
        p.add_argument("--repo", required=True)
        p.add_argument("--config", required=True)
        p.add_argument("--base")
        p.add_argument("--branch")
        p.add_argument("--worktree")
        p.add_argument("--refresh", action="store_true")
        p.add_argument("--push", action="store_true")
        p.add_argument("--report")
        p.add_argument("--timeout", type=int, default=60)
    p = sub.add_parser("containerize")
    p.add_argument("--repo", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--image")
    p.add_argument("--timeout", type=int, default=60)
    p = sub.add_parser("check")
    p.add_argument("--repo", required=True)
    p.add_argument("--config", required=True)
    p = sub.add_parser("make-artifact")
    p.add_argument("--config", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--digest", required=True)
    p.add_argument("--build-id", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("verify-artifact")
    p.add_argument("--config", required=True)
    p.add_argument("--artifact", required=True)
    p.add_argument("--azure-output", action="store_true")
    p.add_argument("--expected-source-revision")
    p.add_argument("--expected-build-id")
    p = sub.add_parser("inventory")
    p.add_argument("--subscription", required=True)
    p.add_argument("--resource-group", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            require(Path(args.app_repo).resolve() != Path(args.infra_repo).resolve(), "Application and infrastructure must be different repositories")
            args.repo, args.base, args.branch, args.worktree = args.infra_repo, args.infra_base, None, None
            prepare_role(args, "infra")
            args.repo, args.base = args.app_repo, args.app_base
            return prepare_role(args, "app")
        if args.operation == "prepare-app": return prepare_role(args, "app")
        if args.operation == "prepare-infra": return prepare_role(args, "infra")
        if args.operation == "inventory": inventory(args)
        elif args.operation == "demo": return demo(args)
        elif args.operation == "make-artifact":
            cfg = read_config(args.config)
            result = json.loads(Path(args.report).read_text())
            require(result.get("containerization") == "passed", "A successful runtime report is required")
            data = {"schema_version": 1, "repository": cfg["azure"]["acr_name"] + ".azurecr.io/" + cfg["app"]["name"], "digest": args.digest, "source_revision": result["source_revision"], "containerization": "passed", "build_id": args.build_id}
            validate_artifact(cfg, data)
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, indent=2) + "\n")
            print("Immutable application image artifact written")
        elif args.operation == "verify-artifact":
            data = validate_artifact(read_config(args.config), json.loads(Path(args.artifact).read_text()))
            if args.expected_source_revision: require(data["source_revision"] == args.expected_source_revision, "Artifact source revision differs from triggering run")
            if args.expected_build_id: require(data["build_id"] == args.expected_build_id, "Artifact build ID differs from triggering run")
            if args.azure_output: print("##vso[task.setvariable variable=imageDigest;isOutput=true]" + data["digest"])
            else: print(json.dumps(data, indent=2))
        else:
            cfg = read_config(args.config)
            repo = Path(args.repo).resolve()
            if args.operation == "containerize":
                result = containerize(repo, cfg, git(repo, "rev-parse", "HEAD"), args.report, args.image, args.timeout)
                print(json.dumps(result, indent=2))
                return 0 if result["containerization"] == "passed" else 2
            source_check(repo, cfg)
            result = report(cfg, git(repo, "rev-parse", "HEAD"))
            print(json.dumps(result, indent=2))
            return 2 if result["blockers"] else 0
        return 0
    except (MigrationError, OSError, ValueError, KeyError, TypeError) as exc:
        print("Migration preparation stopped: " + str(exc), file=sys.stderr)
        return 1

if __name__ == "__main__": sys.exit(main())
