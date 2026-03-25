# GitLab Workshop Operator for Educates (Kopf)

Lightweight namespaced Kubernetes operator that automates GitLab provisioning for [Educates Training Platform](https://github.com/educates/educates-training-platform) workshops.

## Custom Resources

- **`GitLabInstance`** — installs/upgrades a Helm-based GitLab release in the CR namespace.
- **`GitLabUser`** — ensures a GitLab user exists and optionally bootstraps repositories.
  - Username is derived from `metadata.name` (no `spec.username` field).
  - Optional `spec.repositories[]` provisions per-user repositories during reconcile.

## Repository Layout

```
├── operator/              # Operator source code
│   ├── main.py            # Kopf handlers and helpers
│   ├── Dockerfile         # Multi-arch container image (Fedora + Helm + uv)
│   ├── gitlab-values.yaml # Default Helm values for GitLab chart
│   ├── pyproject.toml     # Python dependencies
│   └── uv.lock
├── charts/
│   └── educates-gitlab-operator/  # Helm chart for deploying the operator
├── lab-gitlab-operator/           # Educates workshop content
└── Justfile                       # Task automation
```

## Deploy with Helm

CRDs are shipped with the chart and installed automatically.

```bash
helm upgrade --install educates-gitlab-operator ./charts/educates-gitlab-operator \
  --namespace gitlab-operator-system \
  --create-namespace
```

### Operator-level TLS/CA Configuration

The operator supports cluster-wide TLS defaults so workshop users don't need to configure certificates per instance. Set these via Helm values:

```bash
helm upgrade --install educates-gitlab-operator ./charts/educates-gitlab-operator \
  --namespace gitlab-operator-system \
  --create-namespace \
  --set gitlab.tlsSecretName=wildcard-tls \
  --set gitlab.caSecretName=educates-ca
```

| Helm Value | Env Var | Description |
|---|---|---|
| `gitlab.tlsSecretName` | `GITLAB_TLS_SECRET_NAME` | Default TLS secret for GitLab ingress |
| `gitlab.caSecretName` | `GITLAB_CA_SECRET_NAME` | CA secret for operator API calls |
| `gitlab.caSecretKey` | `GITLAB_CA_SECRET_KEY` | Key within the CA secret (default: `ca.crt`) |
| `gitlab.insecureSkipTlsVerify` | `GITLAB_INSECURE_SKIP_TLS_VERIFY` | Skip TLS verification (disposable envs only) |

Instance CR fields (`spec.tlsSecretRef`, `spec.caSecretRef`, `spec.insecureSkipTlsVerify`) override these defaults when set.

### Install from GHCR (OCI)

After CI publishes the chart:

```bash
helm install educates-gitlab-operator \
  oci://ghcr.io/jorgemoralespou/charts/educates-gitlab-operator \
  --version main \
  --namespace gitlab-operator-system \
  --create-namespace
```

## Local Development

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [just](https://github.com/casey/just) (optional, task runner)
- A Kubernetes cluster with kubeconfig configured

### Build and run

```bash
# Build and push image to local registry
just build-image

# Install via Helm (local chart)
just install

# Or install with custom values
just install --set gitlab.tlsSecretName=my-tls --set gitlab.tlsCAName=my-ca --set image.tag=dev

# Or create a values.yaml file
just install

# Run operator locally (outside cluster)
just run

# Format code
just format
```

### Helm commands

```bash
# Render templates (dry-run)
just template

# Uninstall
just uninstall
```

## How It Works

- On **`GitLabInstance` reconcile**: merges default + user Helm values, runs `helm upgrade --install --wait`, generates a root PAT via the GitLab toolbox pod, stores it in a Kubernetes secret.
- On **`GitLabUser` reconcile**: waits for instance readiness, creates/updates the GitLab user via REST API, provisions any declared repositories.
- On **deletion**: cascades user cleanup before uninstalling the Helm release; best-effort remote cleanup when the instance is already gone.

### Values Precedence

1. Operator defaults (`operator/gitlab-values.yaml`)
2. `spec.valuesYaml` or `spec.values` overlay
3. Explicit field overlays: `spec.domain`, TLS/CA from CR or operator env vars

### Ingress Hostnames

Generated from `metadata.name` using the chart's `hostSuffix` mechanism:
- `gitlab-<instance>.<domain>`
- `registry-<instance>.<domain>`
- `minio-<instance>.<domain>`
