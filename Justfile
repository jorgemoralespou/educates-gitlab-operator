default:
    @just --list

chart_path := "./charts/educates-gitlab-operator"
helm_release := "educates-gitlab-operator"
helm_namespace := "educates-gitlab-operator"

# Build and push operator container image to local registry
build-image:
    docker build -t localhost:5001/educates-gitlab-operator:latest -f operator/Dockerfile . --push

# Install operator via Helm chart
install *ARGS:
    helm upgrade --install {{helm_release}} {{chart_path}} --namespace {{helm_namespace}} --create-namespace $([ -f values.yaml ] && echo "-f values.yaml") {{ARGS}}

# Uninstall operator Helm release
uninstall:
    helm uninstall {{helm_release}} --namespace {{helm_namespace}}

# Render Helm templates locally (dry-run)
template *ARGS:
    helm template {{helm_release}} {{chart_path}} --namespace {{helm_namespace}} $([ -f values.yaml ] && echo "-f values.yaml")  {{ARGS}}

# Run operator locally (requires kubeconfig)
run:
    cd operator && uv run kopf run --standalone --all-namespaces --liveness=http://0.0.0.0:8080/healthz main.py

# Format Python code
format:
    cd operator && uv run black --exclude='.venv' .
