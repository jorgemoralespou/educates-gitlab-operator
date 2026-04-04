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
  --namespace educates-gitlab-operator \
  --create-namespace
```

### cert-manager Integration

The operator uses [cert-manager](https://cert-manager.io) to manage TLS certificates automatically. cert-manager must already be installed in the cluster (it is **not** installed by this chart).

For each `GitLabInstance` the operator:

1. Creates a `cert-manager.io/v1` `Certificate` resource covering all GitLab hostnames (`gitlab-<name>.<domain>`, `registry-<name>.<domain>`, `minio-<name>.<domain>`).
2. Waits for cert-manager to populate the TLS secret (`<instance-name>-tls`) before running the Helm install.
3. Passes the secret to the GitLab chart for ingress TLS and custom CA trust, and mounts it into runner job pods so they can verify GitLab API calls.

#### Default ClusterIssuer

The operator default is a `ClusterIssuer` named `educateswildcard`. Override it cluster-wide at install time:

```bash
helm upgrade --install educates-gitlab-operator ./charts/educates-gitlab-operator \
  --namespace educates-gitlab-operator \
  --create-namespace \
  --set gitlab.certManagerIssuerName=my-cluster-issuer
```

| Helm Value | Env Var | Description |
|---|---|---|
| `gitlab.certManagerIssuerName` | `GITLAB_CERT_MANAGER_ISSUER_NAME` | Default `ClusterIssuer` name (default: `educateswildcard`) |
| `gitlab.insecureSkipTlsVerify` | `GITLAB_INSECURE_SKIP_TLS_VERIFY` | Skip TLS verification (disposable envs only) |

#### Per-instance Override

Use `spec.certManagerIssuerRef` on a `GitLabInstance` to override the issuer for that instance:

```yaml
spec:
  domain: educates.test
  certManagerIssuerRef:
    name: my-other-issuer
    kind: ClusterIssuer  # default when omitted
```

### Install from GHCR (OCI)

After CI publishes the chart:

```bash
helm install educates-gitlab-operator \
  oci://ghcr.io/jorgemoralespou/charts/educates-gitlab-operator \
  --version main \
  --namespace educates-gitlab-operator \
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

# Or install with a custom cert-manager issuer
just install --set gitlab.certManagerIssuerName=my-cluster-issuer --set image.tag=dev

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

- On **`GitLabInstance` reconcile**: creates/updates a cert-manager `Certificate` resource and waits for the TLS secret to be ready; merges default + user Helm values, runs `helm upgrade --install --wait`, generates a root PAT via the GitLab toolbox pod, stores it in a Kubernetes secret.
- On **`GitLabUser` reconcile**: waits for instance readiness, creates/updates the GitLab user via REST API, provisions any declared repositories.
- On **deletion**: cascades user cleanup before uninstalling the Helm release; best-effort remote cleanup when the instance is already gone.

### Values Precedence

1. Operator defaults (`operator/gitlab-values.yaml`)
2. `spec.valuesYaml` or `spec.values` overlay
3. Explicit field overlays: `spec.domain`, cert-manager TLS secret name, runner CA volume

### Ingress Hostnames

Generated from `metadata.name` using the chart's `hostSuffix` mechanism:
- `gitlab-<instance>.<domain>`
- `registry-<instance>.<domain>`
- `minio-<instance>.<domain>`
