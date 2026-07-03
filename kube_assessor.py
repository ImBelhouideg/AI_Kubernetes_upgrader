"""
Kubernetes Upgrade Readiness Assessor
======================================
Collecte les données réelles du cluster via kubectl,
les envoie à un LLM (OpenRouter) pour une analyse experte,
et génère un rapport Markdown complet.

Usage:
    python kube_assessor.py -s 1.29 -t 1.30 -o report.md

Requirements:
    pip install requests
    kubectl configuré pour le cluster cible
    Variable d'environnement: OPENROUTER_API_KEY=sk-or-...
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat")
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"

# APIs connues supprimées ou dépréciées (table statique de base)
REMOVED_APIS: Dict[str, str] = {
    "extensions/v1beta1":                      "1.16",
    "admissionregistration.k8s.io/v1beta1":    "1.22",
    "apiextensions.k8s.io/v1beta1":            "1.22",
    "networking.k8s.io/v1beta1":               "1.22",
    "rbac.authorization.k8s.io/v1beta1":       "1.22",
    "authentication.k8s.io/v1beta1":           "1.22",
    "authorization.k8s.io/v1beta1":            "1.22",
    "certificates.k8s.io/v1beta1":             "1.22",
    "coordination.k8s.io/v1beta1":             "1.22",
    "storage.k8s.io/v1beta1":                  "1.22",
    "policy/v1beta1":                          "1.25",
    "batch/v1beta1":                           "1.25",
    "flowcontrol.apiserver.k8s.io/v1beta1":    "1.26",
    "autoscaling/v2beta1":                     "1.26",
    "autoscaling/v2beta2":                     "1.26",
}

DEPRECATED_APIS: Dict[str, str] = {
    "autoscaling/v1":                          "1.26",
    "flowcontrol.apiserver.k8s.io/v1beta2":    "1.29",
    "flowcontrol.apiserver.k8s.io/v1beta3":    "1.32",
}


# ─────────────────────────────────────────────
# KUBECTL CLIENT
# ─────────────────────────────────────────────

class KubectlError(Exception):
    pass


class KubectlClient:
    def __init__(self, kubeconfig: Optional[str] = None, context: Optional[str] = None):
        self.kubeconfig = kubeconfig
        self.context    = context

    def _build_cmd(self, args: List[str]) -> List[str]:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        return cmd + args

    def run(self, args: List[str]) -> Dict[str, Any]:
        cmd  = self._build_cmd(args)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate()
        return {"cmd": " ".join(shlex.quote(p) for p in cmd), "rc": proc.returncode, "out": out, "err": err}

    def run_text(self, args: List[str]) -> str:
        r = self.run(args)
        if r["rc"] != 0:
            raise KubectlError(f"kubectl failed:\n  cmd : {r['cmd']}\n  err : {r['err'].strip()}")
        return r["out"].strip()

    def run_json(self, args: List[str]) -> Dict[str, Any]:
        r = self.run(args + ["-o", "json"])
        if r["rc"] != 0:
            raise KubectlError(f"kubectl failed:\n  cmd : {r['cmd']}\n  err : {r['err'].strip()}")
        try:
            return json.loads(r["out"] or "{}")
        except Exception as exc:
            raise KubectlError(f"JSON parse error: {exc}\n{r['out'][:500]}")

    def safe_text(self, args: List[str]) -> Optional[str]:
        try:
            return self.run_text(args)
        except KubectlError as e:
            print(f"  [WARN] {e}", file=sys.stderr)
            return None

    def safe_json(self, args: List[str]) -> Optional[Dict[str, Any]]:
        try:
            return self.run_json(args)
        except KubectlError as e:
            print(f"  [WARN] {e}", file=sys.stderr)
            return None


# ─────────────────────────────────────────────
# COLLECTE DES DONNÉES DU CLUSTER
# ─────────────────────────────────────────────

def collect_cluster_state(client: KubectlClient) -> Dict[str, Any]:
    """Collecte exhaustive de l'état du cluster (Steps 1-4 du prompt)."""
    print("Collecting cluster state...")

    state: Dict[str, Any] = {}

    print("  [1/10] kubectl version")
    state["version"] = client.safe_text(["version"])

    print("  [2/10] kubectl cluster-info")
    state["cluster_info"] = client.safe_text(["cluster-info"])

    print("  [3/10] kubectl get nodes")
    state["nodes"] = client.safe_json(["get", "nodes"])

    print("  [4/10] kubectl get namespaces")
    state["namespaces"] = client.safe_json(["get", "namespaces"])

    print("  [5/10] kubectl get all -A (workloads)")
    state["workloads"] = client.safe_json(["get", "all", "-A"])

    print("  [6/10] kubectl get crd")
    state["crds"] = client.safe_json(["get", "crd"])

    print("  [7/10] kubectl get crd -o yaml (CRD details)")
    state["crds_yaml"] = client.safe_text(["get", "crd", "-o", "yaml"])

    print("  [8/10] kubectl get validatingwebhookconfigurations")
    state["webhooks_validating"] = client.safe_json(["get", "validatingwebhookconfigurations"])

    print("  [9/10] kubectl get mutatingwebhookconfigurations")
    state["webhooks_mutating"] = client.safe_json(["get", "mutatingwebhookconfigurations"])

    print("  [10/10] kubectl get apiservices")
    state["apiservices"] = client.safe_json(["get", "apiservices"])

    # Métriques (optionnelles — metrics-server peut ne pas être installé)
    state["nodes_top"]  = client.safe_text(["top", "nodes"])
    state["pods_top"]   = client.safe_text(["top", "pods", "-A"])

    # Ressources supplémentaires
    state["deployments"]  = client.safe_json(["get", "deploy", "-A"])
    state["statefulsets"] = client.safe_json(["get", "sts", "-A"])
    state["daemonsets"]   = client.safe_json(["get", "ds", "-A"])
    state["cronjobs"]     = client.safe_json(["get", "cronjobs", "-A"])
    state["storageclasses"]   = client.safe_json(["get", "storageclasses"])
    state["persistentvolumes"]= client.safe_json(["get", "pv"])

    print("Cluster state collected.\n")
    return state


# ─────────────────────────────────────────────
# ANALYSE LOCALE (déterministe, sans LLM)
# ─────────────────────────────────────────────

def parse_version(v: str) -> Tuple[int, int, int]:
    v = v.lstrip("v")
    parts = re.findall(r"\d+", v)
    nums  = [int(x) for x in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])  # type: ignore


def version_between(val: str, source: str, target: str) -> bool:
    return parse_version(source) <= parse_version(val) <= parse_version(target)


def scan_api_issues(workloads: Optional[Dict], source: str, target: str) -> Tuple[List[Dict], List[Dict]]:
    """Détecte les APIs supprimées et dépréciées utilisées dans le cluster."""
    removed    = []
    deprecated = []
    if not workloads or "items" not in workloads:
        return removed, deprecated

    for item in workloads["items"]:
        api  = item.get("apiVersion", "")
        kind = item.get("kind", "")
        name = item.get("metadata", {}).get("name", "?")
        ns   = item.get("metadata", {}).get("namespace", "cluster-scoped")

        if api in REMOVED_APIS and version_between(REMOVED_APIS[api], source, target):
            removed.append({
                "namespace":       ns,
                "name":            name,
                "kind":            kind,
                "api_version":     api,
                "removal_version": REMOVED_APIS[api],
            })

        if api in DEPRECATED_APIS and version_between(DEPRECATED_APIS[api], source, target):
            deprecated.append({
                "namespace":        ns,
                "name":             name,
                "kind":             kind,
                "api_version":      api,
                "deprecated_in":    DEPRECATED_APIS[api],
            })

    return removed, deprecated


def scan_security(workloads: Optional[Dict]) -> Dict[str, int]:
    """Comptage des ressources à risque sécurité."""
    sec = {"hostNetwork": 0, "hostPID": 0, "hostIPC": 0, "privileged": 0, "hostPath": 0}
    if not workloads:
        return sec
    for item in workloads.get("items", []):
        spec = item.get("spec") or {}
        pod  = spec.get("template", {}).get("spec", {})
        if pod.get("hostNetwork"): sec["hostNetwork"] += 1
        if pod.get("hostPID"):     sec["hostPID"]     += 1
        if pod.get("hostIPC"):     sec["hostIPC"]     += 1
        for c in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            if (c.get("securityContext") or {}).get("privileged"):
                sec["privileged"] += 1
        for vol in (pod.get("volumes") or []):
            if vol.get("hostPath"):
                sec["hostPath"] += 1
    return sec


def scan_runtime(nodes: Optional[Dict]) -> Dict[str, List[str]]:
    """Collecte les informations de runtime des nodes."""
    runtimes, oses, kubelets = set(), set(), set()
    if not nodes:
        return {"runtime": [], "os": [], "kubelet_versions": []}
    for node in nodes.get("items", []):
        info = node.get("status", {}).get("nodeInfo", {})
        runtimes.add(info.get("containerRuntimeVersion") or "unknown")
        oses.add(info.get("operatingSystem") or "unknown")
        kubelets.add(info.get("kubeletVersion") or "unknown")
    return {
        "runtime":          sorted(runtimes),
        "os":               sorted(oses),
        "kubelet_versions": sorted(kubelets),
    }


def scan_crds(crds: Optional[Dict]) -> List[Dict]:
    """Inventaire des CRDs installées."""
    result = []
    if not crds:
        return result
    for item in crds.get("items", []):
        name = item.get("metadata", {}).get("name", "?")
        spec = item.get("spec", {})
        versions = [v.get("name") for v in spec.get("versions", [])]
        storage  = next((v.get("name") for v in spec.get("versions", []) if v.get("storage")), None)
        result.append({
            "name":            name,
            "group":           spec.get("group", "?"),
            "kind":            spec.get("names", {}).get("kind", "?"),
            "versions":        versions,
            "storage_version": storage,
            "conversion":      spec.get("conversion", {}).get("strategy", "None"),
        })
    return result


def scan_webhooks(validating: Optional[Dict], mutating: Optional[Dict]) -> Dict[str, Any]:
    """Analyse les admission webhooks."""
    vh = []
    mh = []
    if validating:
        for wh in validating.get("items", []):
            for w in wh.get("webhooks", []):
                vh.append({
                    "name":          w.get("name"),
                    "failurePolicy": w.get("failurePolicy", "Unknown"),
                    "sideEffects":   w.get("sideEffects", "Unknown"),
                })
    if mutating:
        for wh in mutating.get("items", []):
            for w in wh.get("webhooks", []):
                mh.append({
                    "name":          w.get("name"),
                    "failurePolicy": w.get("failurePolicy", "Unknown"),
                    "sideEffects":   w.get("sideEffects", "Unknown"),
                })
    fail_closed = [w for w in vh + mh if w.get("failurePolicy") == "Fail"]
    return {
        "validating":   vh,
        "mutating":     mh,
        "fail_closed":  fail_closed,
    }


def compute_scores(
    removed: List[Dict],
    deprecated: List[Dict],
    sec: Dict[str, int],
    crds: List[Dict],
    webhooks: Dict,
    state: Dict,
) -> Tuple[int, int]:
    """Calcule le readiness score et le confidence score."""
    readiness = 100

    # APIs supprimées = critique
    readiness -= len(removed) * 40

    # APIs dépréciées = moyen
    readiness -= len(deprecated) * 10

    # Sécurité
    if sec.get("privileged", 0) > 0:
        readiness -= 10
    if sec.get("hostPath", 0) > 0:
        readiness -= 5
    if sec.get("hostNetwork", 0) > 0:
        readiness -= 5

    # Webhooks fail-closed = risque
    readiness -= len(webhooks.get("fail_closed", [])) * 10

    readiness = max(0, readiness)

    # Confidence : baisse si données manquantes
    confidence = 100
    if not state.get("version"):
        confidence -= 40
    if not state.get("workloads"):
        confidence -= 30
    if not state.get("nodes"):
        confidence -= 15
    if not state.get("crds"):
        confidence -= 10
    if not state.get("webhooks_validating") and not state.get("webhooks_mutating"):
        confidence -= 5
    # Si pas de métriques = données partielles
    if not state.get("nodes_top"):
        confidence -= 5

    confidence = max(0, confidence)
    return readiness, confidence


# ─────────────────────────────────────────────
# APPEL LLM (OpenRouter)
# ─────────────────────────────────────────────

def build_llm_prompt(
    source: str,
    target: str,
    state: Dict[str, Any],
    removed: List[Dict],
    deprecated: List[Dict],
    sec: Dict[str, int],
    crds: List[Dict],
    webhooks: Dict,
    runtime: Dict,
) -> str:
    """Construit le prompt envoyé au LLM avec toutes les données collectées."""

    crd_summary = json.dumps(crds[:20], indent=2) if crds else "No CRDs found"
    webhook_summary = json.dumps(webhooks, indent=2)
    removed_summary = json.dumps(removed, indent=2) if removed else "None"
    deprecated_summary = json.dumps(deprecated, indent=2) if deprecated else "None"

    # Résumé des nodes
    nodes_summary = "No node data"
    if state.get("nodes"):
        nodes_summary = ""
        for node in state["nodes"].get("items", []):
            n = node.get("metadata", {}).get("name", "?")
            info = node.get("status", {}).get("nodeInfo", {})
            nodes_summary += (
                f"  - {n}: OS={info.get('operatingSystem','?')} "
                f"kernel={info.get('kernelVersion','?')} "
                f"runtime={info.get('containerRuntimeVersion','?')} "
                f"kubelet={info.get('kubeletVersion','?')}\n"
            )

    # Résumé namespaces
    namespaces = []
    if state.get("namespaces"):
        namespaces = [ns.get("metadata", {}).get("name") for ns in state["namespaces"].get("items", [])]

    prompt = f"""You are a Senior Kubernetes Platform Engineer performing a comprehensive upgrade readiness review.

## Upgrade Context
- Source Version: {source}
- Target Version: {target}
- Assessment Date: {datetime.datetime.utcnow().isoformat()}Z

## Cluster State

### Kubernetes Version
```
{state.get("version", "unavailable")}
```

### Nodes
{nodes_summary}

### Namespaces
{", ".join(namespaces) if namespaces else "unavailable"}

### Runtime Information
- Container Runtimes: {", ".join(runtime.get("runtime", ["unknown"]))}
- Operating Systems: {", ".join(runtime.get("os", ["unknown"]))}
- Kubelet Versions: {", ".join(runtime.get("kubelet_versions", ["unknown"]))}

## API Analysis

### Removed APIs Found in Cluster (CRITICAL)
{removed_summary}

### Deprecated APIs Found in Cluster
{deprecated_summary}

## Security Findings
- hostNetwork pods: {sec.get("hostNetwork", 0)}
- hostPID pods: {sec.get("hostPID", 0)}
- hostIPC pods: {sec.get("hostIPC", 0)}
- Privileged containers: {sec.get("privileged", 0)}
- HostPath volumes: {sec.get("hostPath", 0)}

## CRD Inventory (first 20)
```json
{crd_summary}
```

## Admission Webhooks
```json
{webhook_summary}
```

## Storage
- StorageClasses: {len((state.get("storageclasses") or {}).get("items", []))} found
- PersistentVolumes: {len((state.get("persistentvolumes") or {}).get("items", []))} found

---

Based on ALL the above data, perform a comprehensive upgrade readiness assessment covering:

1. **API Compatibility** — analyze removed/deprecated APIs and their impact
2. **CRD Compatibility** — for each CRD, assess if it will survive the upgrade
3. **Controller/Operator Compatibility** — identify any operators (cert-manager, ingress-nginx, ArgoCD, etc.) detected and their compatibility with the target version
4. **Admission Webhook Risks** — can fail-closed webhooks block workloads?
5. **Networking Risks** — will CNI/CoreDNS/kube-proxy survive?
6. **Storage Risks** — will PVs/CSI drivers remain accessible?
7. **Security Changes** — any PSP/PSA changes, RBAC impacts?
8. **Runtime Risks** — is the container runtime compatible?
9. **Node Upgrade Risks** — kubelet skew, OS compatibility
10. **Control Plane Risks** — etcd, kube-apiserver, scheduler risks

Then produce the following structured report EXACTLY:

# Kubernetes Upgrade Readiness Assessment — LLM Analysis

## Upgrade Simulation Results

| Area | Status | Severity | Explanation |
|---|---|---|---|
| Control Plane | PASS/WARNING/HIGH RISK/CRITICAL | Low/Medium/High/Critical | ... |
| Nodes | ... | ... | ... |
| APIs | ... | ... | ... |
| CRDs | ... | ... | ... |
| Controllers | ... | ... | ... |
| Webhooks | ... | ... | ... |
| Networking | ... | ... | ... |
| Storage | ... | ... | ... |
| Security | ... | ... | ... |
| Runtime | ... | ... | ... |

## Failure Scenario Analysis

For each of the following, answer YES or NO with a detailed reason:

- Could workloads fail to start?
- Could controllers crash?
- Could CRDs become unreadable?
- Could admission webhooks block deployments?
- Could storage become inaccessible?
- Could networking break?
- Could node upgrades fail?
- Could the control plane fail?

## Critical Incompatibilities

For each incompatibility found, state:
- WHAT WILL BREAK:
- WHEN IT WILL BREAK:
- IMPACT:
- SEVERITY:
- REMEDIATION:

## Required Actions Before Upgrade
(numbered list)

## Recommended Upgrade Order
(numbered list)

## Post-Upgrade Validations
(numbered list)

## Final Recommendation
(detailed paragraph)

Be exhaustive, conservative, and evidence-based. Never assume compatibility unless verified from the data provided. If something cannot be confirmed, classify it as a risk.
"""
    return prompt


def call_llm(prompt: str) -> str:
    """Appelle OpenRouter avec le prompt et retourne la réponse texte."""
    try:
        import requests
    except ImportError:
        return "[ERROR] 'requests' library not installed. Run: pip install requests"

    if not OPENROUTER_API_KEY:
        return "[ERROR] OPENROUTER_API_KEY environment variable not set."

    print("Calling LLM (OpenRouter)...")
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization":  f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":   "application/json",
                "HTTP-Referer":   "https://github.com/imadeddinebelhouideg/kube-assessor",
                "X-Title":        "Kubernetes Upgrade Assessor",
            },
            json={
                "model":      OPENROUTER_MODEL,
                "max_tokens": 4096,
                "messages": [
                    {
                        "role":    "system",
                        "content": (
                            "You are a Senior Kubernetes Platform Engineer with 10+ years of experience "
                            "in production cluster upgrades. You produce exhaustive, evidence-based, "
                            "conservative upgrade readiness reports. You never hide risks or assume "
                            "compatibility without evidence."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        return f"[ERROR] LLM call failed: {exc}"


# ─────────────────────────────────────────────
# GÉNÉRATION DU RAPPORT FINAL
# ─────────────────────────────────────────────

def generate_report(
    source: str,
    target: str,
    state: Dict[str, Any],
    removed: List[Dict],
    deprecated: List[Dict],
    sec: Dict[str, int],
    crds: List[Dict],
    webhooks: Dict,
    runtime: Dict,
    readiness: int,
    confidence: int,
    llm_analysis: str,
) -> str:
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    if readiness >= 90 and confidence >= 80:
        decision = "APPROVED"
    elif readiness >= 75:
        decision = "CONDITIONAL"
    else:
        decision = "NOT RECOMMENDED"

    lines = []

    # ── En-tête
    lines += [
        "# Kubernetes Upgrade Readiness Assessment",
        "",
        f"Generated : {now}",
        f"Source    : v{source}",
        f"Target    : v{target}",
        f"Model     : {OPENROUTER_MODEL}",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        f"**UPGRADE DECISION : {decision}**",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Readiness Score | {readiness}/100 |",
        f"| Confidence      | {confidence}%   |",
        "",
    ]

    # ── Issues détectées localement
    lines += ["## Static Analysis Results", ""]

    if removed:
        lines += ["### 🔴 Removed APIs (CRITICAL)", ""]
        lines += ["| Namespace | Kind | Name | API Version | Removed In |",
                  "|-----------|------|------|-------------|------------|"]
        for r in removed:
            lines.append(f"| {r['namespace']} | {r['kind']} | {r['name']} | {r['api_version']} | {r['removal_version']} |")
        lines.append("")
    else:
        lines += ["### ✅ Removed APIs", "- None detected", ""]

    if deprecated:
        lines += ["### 🟡 Deprecated APIs", ""]
        lines += ["| Namespace | Kind | Name | API Version | Deprecated In |",
                  "|-----------|------|------|-------------|---------------|"]
        for d in deprecated:
            lines.append(f"| {d['namespace']} | {d['kind']} | {d['name']} | {d['api_version']} | {d['deprecated_in']} |")
        lines.append("")
    else:
        lines += ["### ✅ Deprecated APIs", "- None detected", ""]

    # ── Sécurité
    lines += [
        "### Security Findings",
        "",
        f"| Check | Count | Risk |",
        f"|-------|-------|------|",
        f"| hostNetwork pods      | {sec.get('hostNetwork',0)} | Medium |",
        f"| hostPID pods          | {sec.get('hostPID',0)}     | High   |",
        f"| hostIPC pods          | {sec.get('hostIPC',0)}     | High   |",
        f"| Privileged containers | {sec.get('privileged',0)}  | High   |",
        f"| HostPath volumes      | {sec.get('hostPath',0)}    | Medium |",
        "",
    ]

    # ── CRDs
    lines += ["### CRD Inventory", ""]
    if crds:
        lines += ["| CRD Name | Group | Kind | Versions | Storage Version | Conversion |",
                  "|----------|-------|------|----------|-----------------|------------|"]
        for c in crds:
            lines.append(
                f"| {c['name']} | {c['group']} | {c['kind']} "
                f"| {', '.join(c['versions'])} | {c['storage_version']} | {c['conversion']} |"
            )
        lines.append("")
    else:
        lines += ["- No CRDs found", ""]

    # ── Webhooks
    lines += ["### Admission Webhooks", ""]
    all_wh = webhooks.get("validating", []) + webhooks.get("mutating", [])
    if all_wh:
        lines += ["| Name | Type | FailurePolicy | SideEffects |",
                  "|------|------|---------------|-------------|"]
        for w in webhooks.get("validating", []):
            lines.append(f"| {w['name']} | Validating | {w['failurePolicy']} | {w['sideEffects']} |")
        for w in webhooks.get("mutating", []):
            lines.append(f"| {w['name']} | Mutating | {w['failurePolicy']} | {w['sideEffects']} |")
        lines.append("")
        if webhooks.get("fail_closed"):
            lines.append(f"> ⚠️ **{len(webhooks['fail_closed'])} webhook(s) with FailurePolicy=Fail** — these can block all deployments if they become unavailable during upgrade.")
            lines.append("")
    else:
        lines += ["- No admission webhooks found", ""]

    # ── Runtime
    lines += [
        "### Runtime Information",
        "",
        f"- Container Runtimes : {', '.join(runtime.get('runtime', ['unknown']))}",
        f"- Operating Systems  : {', '.join(runtime.get('os', ['unknown']))}",
        f"- Kubelet Versions   : {', '.join(runtime.get('kubelet_versions', ['unknown']))}",
        "",
    ]

    # ── Version
    lines += [
        "### Cluster Version",
        "```",
        state.get("version", "unavailable"),
        "```",
        "",
        "---",
        "",
    ]

    # ── Analyse LLM
    lines += [
        "## LLM Expert Analysis",
        "",
        llm_analysis,
        "",
        "---",
        "",
        "*Report generated by Kubernetes Upgrade Readiness Assessor*",
        f"*LLM: {OPENROUTER_MODEL} via OpenRouter*",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kubernetes Upgrade Readiness Assessor — kubectl + LLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-s", "--source",     required=True, help="Source Kubernetes version, e.g. 1.29")
    parser.add_argument("-t", "--target",     required=True, help="Target Kubernetes version, e.g. 1.30")
    parser.add_argument("-k", "--kubeconfig", help="Path to kubeconfig file")
    parser.add_argument("-c", "--context",    help="kubectl context to use")
    parser.add_argument("-o", "--output",     help="Output file (default: stdout)")
    parser.add_argument("--no-llm",           action="store_true", help="Skip LLM analysis (static analysis only)")
    parser.add_argument("--model",            default=None, help="OpenRouter model to use")
    args = parser.parse_args(argv)

    if args.model:
        OPENROUTER_MODEL = args.model

    print(f"\n{'='*60}")
    print(f"  Kubernetes Upgrade Readiness Assessor")
    print(f"  {args.source} → {args.target}")
    print(f"{'='*60}\n")

    # 1. Collecte kubectl
    client = KubectlClient(kubeconfig=args.kubeconfig, context=args.context)
    state  = collect_cluster_state(client)

    # 2. Analyse statique locale
    print("Running static analysis...")
    removed, deprecated = scan_api_issues(state.get("workloads"), args.source, args.target)
    sec      = scan_security(state.get("workloads"))
    crds     = scan_crds(state.get("crds"))
    webhooks = scan_webhooks(state.get("webhooks_validating"), state.get("webhooks_mutating"))
    runtime  = scan_runtime(state.get("nodes"))

    readiness, confidence = compute_scores(removed, deprecated, sec, crds, webhooks, state)

    print(f"  Removed APIs    : {len(removed)}")
    print(f"  Deprecated APIs : {len(deprecated)}")
    print(f"  CRDs            : {len(crds)}")
    print(f"  Webhooks        : {len(webhooks.get('validating',[])) + len(webhooks.get('mutating',[]))}")
    print(f"  Readiness Score : {readiness}/100")
    print(f"  Confidence      : {confidence}%")
    print()

    # 3. Analyse LLM
    if args.no_llm:
        llm_analysis = "> LLM analysis skipped (--no-llm flag)."
    else:
        prompt       = build_llm_prompt(args.source, args.target, state, removed, deprecated, sec, crds, webhooks, runtime)
        llm_analysis = call_llm(prompt)

    # 4. Génération du rapport
    print("Generating report...")
    report = generate_report(
        source=args.source,
        target=args.target,
        state=state,
        removed=removed,
        deprecated=deprecated,
        sec=sec,
        crds=crds,
        webhooks=webhooks,
        runtime=runtime,
        readiness=readiness,
        confidence=confidence,
        llm_analysis=llm_analysis,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"\n✅ Report written to: {args.output}")
    else:
        print("\n" + report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())