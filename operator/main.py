#!/usr/bin/env python3
"""Kopf-based GitLab workshop operator.

This operator manages two namespaced custom resources:
- GitLabInstance: installs/upgrades a GitLab Helm release.
- GitLabUser: creates/deletes users and optional per-user repositories.
"""

import base64
import copy
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

import kopf
import kubernetes
import requests
import yaml
from kubernetes.stream import stream

logger = logging.getLogger(__name__)

GROUP = "gitlab.operators.educates.dev"
VERSION = "v1beta1"
DEFAULT_VALUES_PATH_ENV = "GITLAB_DEFAULT_VALUES_PATH"
DEFAULT_VALUES_CANDIDATES = [
    "/opt/app-root/gitlab-values.yaml",
    "/opt/app-root/default-gitlab-values.yaml",
    "gitlab-values.yaml",
]
INTERNAL_CHART = "gitlab/gitlab"
INTERNAL_EDITION = "ce"
DEFAULT_BOOTSTRAP_PAT_SECRET_SUFFIX = "root-bootstrap-pat"
DEFAULT_BOOTSTRAP_PAT_KEY = "token"
DEFAULT_BOOTSTRAP_PAT_NAME = "workshop-bootstrap"
DEFAULT_BOOTSTRAP_PAT_EXPIRES_DAYS = 30

# Operator-level cert-manager configuration (environment variables).
# Instance CR spec.certManagerIssuerRef overrides these when set.
OPERATOR_CERT_MANAGER_ISSUER_NAME_ENV = "GITLAB_CERT_MANAGER_ISSUER_NAME"
OPERATOR_INSECURE_SKIP_TLS_VERIFY_ENV = "GITLAB_INSECURE_SKIP_TLS_VERIFY"
DEFAULT_CERT_MANAGER_ISSUER_NAME = "educateswildcard"


def _operator_cert_manager_issuer_name() -> str:
    return os.getenv(OPERATOR_CERT_MANAGER_ISSUER_NAME_ENV, DEFAULT_CERT_MANAGER_ISSUER_NAME)


def _operator_insecure_skip_tls() -> bool:
    return os.getenv(OPERATOR_INSECURE_SKIP_TLS_VERIFY_ENV, "").lower() in (
        "true",
        "1",
        "yes",
    )


def _instance_tls_secret_name(instance_name: str) -> str:
    return f"{instance_name}-tls"


def _resolve_cert_manager_issuer(spec: Dict[str, Any]) -> Dict[str, str]:
    """Resolve cert-manager issuer: instance CR > operator env > default."""
    instance_ref = spec.get("certManagerIssuerRef")
    if isinstance(instance_ref, dict) and instance_ref.get("name"):
        return {
            "name": instance_ref["name"],
            "kind": instance_ref.get("kind", "ClusterIssuer"),
        }
    return {
        "name": _operator_cert_manager_issuer_name(),
        "kind": "ClusterIssuer",
    }


# ---- Shell/command helpers -------------------------------------------------
def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def _run_or_retry(command: list[str], context: str) -> None:
    try:
        _run(command)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        details = stderr or stdout or str(exc)
        raise kopf.TemporaryError(f"{context} failed: {details}", delay=20) from exc


def _instance_release_name(body: Dict[str, Any]) -> str:
    # Internal release naming: one release per GitLabInstance object.
    return body["metadata"]["name"]


def _instance_chart(body: Dict[str, Any]) -> str:
    return INTERNAL_CHART


def _instance_edition(body: Dict[str, Any]) -> str:
    return INTERNAL_EDITION


def _instance_gitlab_url(body: Dict[str, Any]) -> str:
    # Explicit URL wins; otherwise derive stable URL from instance name + domain.
    explicit = body.get("spec", {}).get("gitlabUrl")
    if explicit:
        return explicit.rstrip("/")
    domain = body.get("spec", {}).get("domain")
    if not domain:
        raise kopf.TemporaryError(
            "GitLabInstance requires spec.domain or spec.gitlabUrl", delay=10
        )
    instance_name = body.get("metadata", {}).get("name", "gitlab")
    return f"https://gitlab-{instance_name}.{domain}"


def _instance_internal_url(instance: Dict[str, Any], namespace: str) -> str:
    instance_name = instance.get("metadata", {}).get("name", "gitlab")
    return f"http://{instance_name}-webservice-default.{namespace}:8080"


def _instance_bootstrap_pat_secret_name(instance_name: str) -> str:
    return f"{instance_name}-{DEFAULT_BOOTSTRAP_PAT_SECRET_SUFFIX}"


def _instance_tls_verify(body: Dict[str, Any]) -> bool:
    spec = body.get("spec", {})
    # Instance CR field wins; otherwise fall back to operator env var.
    if "insecureSkipTlsVerify" in spec:
        return not bool(spec["insecureSkipTlsVerify"])
    return not _operator_insecure_skip_tls()


def _instance_verify_arg(
    instance: Dict[str, Any], namespace: str
) -> Tuple[bool | str, Optional[str]]:
    if not _instance_tls_verify(instance):
        return False, None

    instance_name = instance["metadata"]["name"]
    tls_secret_name = _instance_tls_secret_name(instance_name)
    v1 = kubernetes.client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(tls_secret_name, namespace)
    except kubernetes.client.exceptions.ApiException:
        return True, None

    if not secret.data or "ca.crt" not in secret.data:
        return True, None

    ca_pem = base64.b64decode(secret.data["ca.crt"]).decode("utf-8")
    fd, ca_path = tempfile.mkstemp(prefix="gitlab-ca-", suffix=".crt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(ca_pem)
    return ca_path, ca_path


def _instance_values_file(body: Dict[str, Any]) -> str:
    # Build final Helm values by combining defaults, user overrides and
    # operator-enforced overlays, then persist to a temp file for helm -f.
    spec = body.get("spec", {})
    instance_name = body.get("metadata", {}).get("name", "gitlab")
    defaults = _load_default_values()
    custom = _load_instance_values(spec)
    merged = _deep_merge(defaults, custom)
    final_values = _apply_explicit_overlays(merged, spec, instance_name)
    fd, path = tempfile.mkstemp(prefix="gitlab-values-", suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(final_values, f, sort_keys=False)
    return path


def _load_default_values() -> Dict[str, Any]:
    # Resolve default values file from env override first, then fallbacks.
    env_path = os.getenv(DEFAULT_VALUES_PATH_ENV)
    candidates = [env_path] if env_path else []
    candidates.extend(DEFAULT_VALUES_CANDIDATES)
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                raise kopf.PermanentError(
                    f"Default values file must be a YAML map: {candidate}"
                )
            return data
    raise kopf.PermanentError(
        f"Default values file not found. Set {DEFAULT_VALUES_PATH_ENV} or provide one of: {DEFAULT_VALUES_CANDIDATES}"
    )


def _load_instance_values(spec: Dict[str, Any]) -> Dict[str, Any]:
    if "values" in spec and "valuesYaml" in spec:
        raise kopf.PermanentError("Use only one of spec.values or spec.valuesYaml.")
    if "valuesYaml" in spec:
        loaded = yaml.safe_load(spec["valuesYaml"]) or {}
        if not isinstance(loaded, dict):
            raise kopf.PermanentError(
                "spec.valuesYaml must parse to a YAML map/object."
            )
        return loaded
    values = spec.get("values", {})
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise kopf.PermanentError("spec.values must be a map/object.")
    return values


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _ensure_map(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    existing = parent.get(key)
    if not isinstance(existing, dict):
        parent[key] = {}
    return parent[key]


def _apply_explicit_overlays(
    values: Dict[str, Any], spec: Dict[str, Any], instance_name: str
) -> Dict[str, Any]:
    merged = copy.deepcopy(values)

    domain = (spec.get("domain") or "").strip()
    tls_secret_name = _instance_tls_secret_name(instance_name)

    global_cfg = _ensure_map(merged, "global")
    hosts_cfg = _ensure_map(global_cfg, "hosts")
    ingress_cfg = _ensure_map(global_cfg, "ingress")
    gitlab_host_cfg = _ensure_map(hosts_cfg, "gitlab")
    registry_host_cfg = _ensure_map(hosts_cfg, "registry")
    minio_host_cfg = _ensure_map(hosts_cfg, "minio")

    # Domain is required by CRD; always force it as final overlay.
    if not domain:
        raise kopf.PermanentError(
            "GitLabInstance spec.domain is required and must be non-empty."
        )
    hosts_cfg["domain"] = domain

    # Use the chart-native hostSuffix to enforce:
    #   <service>-<instance_name>.<domain>
    # e.g. gitlab-workshop-gitlab.educates.test
    hosts_cfg["hostSuffix"] = instance_name
    # Do not set explicit host `name`; in this chart it is interpreted as a full
    # host override and bypasses domain/suffix assembly.
    gitlab_host_cfg.pop("name", None)
    registry_host_cfg.pop("name", None)
    minio_host_cfg.pop("name", None)

    # TLS: use the cert-manager-issued secret (created by the operator before helm install).
    tls_cfg = _ensure_map(ingress_cfg, "tls")
    tls_cfg["secretName"] = tls_secret_name

    # CA trust for all GitLab pods (including the runner pod).
    # The cert-manager TLS secret contains ca.crt which GitLab injects into
    # each pod's system trust store via its certificate init container.
    certs_cfg = _ensure_map(global_cfg, "certificates")
    certs_cfg["customCAs"] = [{"secret": tls_secret_name}]

    # Runner job pods: mount the cert-manager TLS secret so job containers
    # can call update-ca-certificates and trust the GitLab CA.
    runner_cfg = _ensure_map(merged, "gitlab-runner")
    runner_cfg["runners"] = {
        "config": (
            "[[runners]]\n"
            "  [runners.kubernetes]\n"
            "    privileged = true\n"
            "  [[runners.kubernetes.volumes.secret]]\n"
            f'    name = "{tls_secret_name}"\n'
            '    mount_path = "/usr/local/share/ca-certificates/"\n'
            "    read_only = true\n"
        )
    }

    return merged


# ---- cert-manager helpers --------------------------------------------------
def _ensure_certificate(instance: Dict[str, Any], namespace: str) -> str:
    """Create or update a cert-manager Certificate for this GitLab instance.

    Returns the TLS secret name that cert-manager will populate.
    """
    instance_name = instance["metadata"]["name"]
    domain = instance.get("spec", {}).get("domain", "")
    spec = instance.get("spec", {})
    issuer = _resolve_cert_manager_issuer(spec)
    tls_secret_name = _instance_tls_secret_name(instance_name)

    dns_names = [
        f"gitlab-{instance_name}.{domain}",
        f"registry-{instance_name}.{domain}",
        f"minio-{instance_name}.{domain}",
    ]

    cert_body: Dict[str, Any] = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {
            "name": tls_secret_name,
            "namespace": namespace,
        },
        "spec": {
            "secretName": tls_secret_name,
            "issuerRef": {
                "name": issuer["name"],
                "kind": issuer["kind"],
            },
            "dnsNames": dns_names,
        },
    }

    owner_ref = _owner_reference_for_instance(instance)
    if owner_ref.get("uid"):
        cert_body["metadata"]["ownerReferences"] = [owner_ref]

    logger.info(
        "Ensuring cert-manager Certificate '%s' in namespace '%s' "
        "using %s '%s' for domains: %s",
        tls_secret_name,
        namespace,
        issuer["kind"],
        issuer["name"],
        ", ".join(dns_names),
    )

    api = kubernetes.client.CustomObjectsApi()
    try:
        api.create_namespaced_custom_object(
            group="cert-manager.io",
            version="v1",
            namespace=namespace,
            plural="certificates",
            body=cert_body,
        )
        logger.info("Created Certificate '%s'.", tls_secret_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 409:
            api.patch_namespaced_custom_object(
                group="cert-manager.io",
                version="v1",
                namespace=namespace,
                plural="certificates",
                name=tls_secret_name,
                body=cert_body,
            )
            logger.info("Updated existing Certificate '%s'.", tls_secret_name)
        else:
            raise kopf.TemporaryError(
                f"Failed to create Certificate '{tls_secret_name}': {exc}", delay=20
            ) from exc

    return tls_secret_name


def _ensure_tls_secret_ready(namespace: str, secret_name: str) -> None:
    """Raise TemporaryError if the cert-manager TLS secret is not yet populated."""
    v1 = kubernetes.client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(secret_name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            raise kopf.TemporaryError(
                f"Waiting for cert-manager to create TLS secret '{secret_name}'. "
                "Check the Certificate resource status and ClusterIssuer configuration.",
                delay=15,
            ) from exc
        raise kopf.TemporaryError(
            f"Error reading TLS secret '{secret_name}': {exc}", delay=10
        ) from exc
    if not secret.data or "tls.crt" not in secret.data or "tls.key" not in secret.data:
        raise kopf.TemporaryError(
            f"TLS secret '{secret_name}' is not yet fully populated by cert-manager.",
            delay=10,
        )


# ---- Kubernetes API helpers ------------------------------------------------
def _resolve_instance_ref(
    instance_ref: Any, default_namespace: str
) -> tuple[str, str]:
    """Return (name, namespace) from an instanceRef object."""
    if isinstance(instance_ref, dict):
        return instance_ref["name"], instance_ref.get("namespace", default_namespace)
    # Fallback for legacy string values (should not occur after CRD migration).
    return str(instance_ref), default_namespace


def _get_instance(namespace: str, name: str) -> Dict[str, Any]:
    api = kubernetes.client.CustomObjectsApi()
    return api.get_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural="gitlabinstances",
        name=name,
    )


def _list_users_for_instance(
    namespace: str, instance_name: str
) -> list[Dict[str, Any]]:
    api = kubernetes.client.CustomObjectsApi()
    users = api.list_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural="gitlabusers",
    )
    items = users.get("items", []) if isinstance(users, dict) else []
    def _matches(user: Dict[str, Any]) -> bool:
        ref = user.get("spec", {}).get("instanceRef")
        if isinstance(ref, dict):
            return ref.get("name") == instance_name
        return ref == instance_name

    return [u for u in items if _matches(u)]


def _delete_users_for_instance(namespace: str, instance_name: str) -> int:
    # Best-effort cascading delete for dependents in case ownerReferences are
    # missing on older resources.
    api = kubernetes.client.CustomObjectsApi()
    users = _list_users_for_instance(namespace, instance_name)
    for user in users:
        user_name = user.get("metadata", {}).get("name")
        if not user_name:
            continue
        try:
            api.delete_namespaced_custom_object(
                group=GROUP,
                version=VERSION,
                namespace=namespace,
                plural="gitlabusers",
                name=user_name,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise kopf.TemporaryError(
                    f"Failed deleting dependent GitLabUser '{user_name}': {exc}",
                    delay=10,
                ) from exc
    return len(users)


def _secret_value(namespace: str, secret_name: str, key: str) -> str:
    v1 = kubernetes.client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(secret_name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            raise kopf.TemporaryError(
                f"Required secret '{secret_name}' was not found in namespace '{namespace}'.",
                delay=20,
            ) from exc
        raise kopf.TemporaryError(
            f"Failed to read secret '{secret_name}' in namespace '{namespace}': {exc}",
            delay=20,
        ) from exc
    if not secret.data or key not in secret.data:
        raise kopf.TemporaryError(f"Missing secret key: {secret_name}/{key}", delay=10)
    return base64.b64decode(secret.data[key]).decode("utf-8")


# ---- PAT bootstrap helpers -------------------------------------------------
def _has_secret_key(namespace: str, secret_name: str, key: str) -> bool:
    v1 = kubernetes.client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(secret_name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            return False
        raise kopf.TemporaryError(
            f"Failed to read secret '{secret_name}' in namespace '{namespace}': {exc}",
            delay=20,
        ) from exc
    return bool(secret.data and key in secret.data and secret.data[key])


def _toolbox_pod_name(namespace: str, release: str) -> str:
    v1 = kubernetes.client.CoreV1Api()
    candidates = [
        f"app=toolbox,release={release}",
        "app=toolbox",
        "app.kubernetes.io/name=toolbox,app.kubernetes.io/instance=" + release,
        "app.kubernetes.io/name=toolbox",
    ]
    for selector in candidates:
        pods = v1.list_namespaced_pod(namespace, label_selector=selector).items
        running = [p for p in pods if (p.status and p.status.phase == "Running")]
        if running:
            return running[0].metadata.name
    raise kopf.TemporaryError(
        f"GitLab toolbox pod not ready in namespace '{namespace}'.",
        delay=20,
    )


def _extract_pat_token(output: str) -> Optional[str]:
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]{20,}", candidate):
            return candidate
    return None


def _generate_root_pat(
    namespace: str, release: str, token_name: str, expires_days: int
) -> str:
    # Generate PAT inside toolbox pod so token creation follows GitLab internals.
    pod_name = _toolbox_pod_name(namespace, release)
    safe_token_name = token_name.replace("\\", "\\\\").replace("'", "\\'")
    ruby = f"""
user = User.find_by_username('root')
raise 'root user not found' unless user
user.personal_access_tokens.where(name: '{safe_token_name}').each(&:revoke!)
token = user.personal_access_tokens.create!(
  name: '{safe_token_name}',
  scopes: [:api],
  expires_at: {expires_days}.days.from_now.to_date
)
token.set_token(SecureRandom.hex(32))
token.save!
puts token.token
""".strip()
    command = ["bash", "-lc", f'gitlab-rails runner "{ruby}"']
    try:
        output = stream(
            kubernetes.client.CoreV1Api().connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container="toolbox",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
    except Exception as exc:
        raise kopf.TemporaryError(
            f"Failed to generate root PAT from toolbox pod: {exc}", delay=20
        ) from exc
    pat = _extract_pat_token(output or "")
    if not pat:
        raise kopf.TemporaryError(
            "Failed to extract generated PAT from toolbox output.", delay=20
        )
    return pat


def _upsert_secret_token(
    namespace: str,
    secret_name: str,
    key: str,
    token: str,
    owner_reference: Optional[Dict[str, Any]] = None,
) -> None:
    v1 = kubernetes.client.CoreV1Api()
    encoded = base64.b64encode(token.encode("utf-8")).decode("utf-8")
    metadata = kubernetes.client.V1ObjectMeta(name=secret_name, namespace=namespace)
    if owner_reference:
        metadata.owner_references = [
            kubernetes.client.V1OwnerReference(
                api_version=owner_reference["apiVersion"],
                kind=owner_reference["kind"],
                name=owner_reference["name"],
                uid=owner_reference["uid"],
                controller=owner_reference.get("controller", False),
                block_owner_deletion=owner_reference.get("blockOwnerDeletion", False),
            )
        ]
    body = kubernetes.client.V1Secret(
        metadata=metadata,
        type="Opaque",
        data={key: encoded},
    )
    try:
        v1.read_namespaced_secret(secret_name, namespace)
        v1.patch_namespaced_secret(secret_name, namespace, body)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise kopf.TemporaryError(
                f"Failed to store bootstrap PAT secret '{secret_name}': {exc}",
                delay=20,
            ) from exc
        v1.create_namespaced_secret(namespace, body)


def _ensure_bootstrap_pat_secret(
    namespace: str,
    release: str,
    instance_name: str,
    owner_reference: Optional[Dict[str, Any]] = None,
) -> str:
    secret_name = _instance_bootstrap_pat_secret_name(instance_name)
    key = DEFAULT_BOOTSTRAP_PAT_KEY
    if _has_secret_key(namespace, secret_name, key):
        return secret_name
    token = _generate_root_pat(
        namespace=namespace,
        release=release,
        token_name=f"{DEFAULT_BOOTSTRAP_PAT_NAME}-{instance_name}",
        expires_days=DEFAULT_BOOTSTRAP_PAT_EXPIRES_DAYS,
    )
    _upsert_secret_token(
        namespace, secret_name, key, token, owner_reference=owner_reference
    )
    return secret_name


def _instance_pat_token(
    *,
    namespace: str,
    instance: Dict[str, Any],
    token_secret_ref: Optional[Dict[str, Any]],
) -> str:
    instance_name = instance.get("metadata", {}).get("name", "gitlab")
    release = _instance_release_name(instance)
    default_secret_name = _instance_bootstrap_pat_secret_name(instance_name)
    ref = token_secret_ref if isinstance(token_secret_ref, dict) else {}
    key = ref.get("key", DEFAULT_BOOTSTRAP_PAT_KEY)
    secret_name = ref.get("name", default_secret_name)

    # Self-healing loop for the operator-managed PAT secret:
    # if deleted/missing later, recreate and read again.
    for _ in range(2):
        try:
            return _secret_value(namespace, secret_name, key).strip()
        except kopf.TemporaryError:
            if secret_name != default_secret_name:
                raise
            _ensure_bootstrap_pat_secret(
                namespace=namespace, release=release, instance_name=instance_name
            )
    raise kopf.TemporaryError(
        f"Failed to read bootstrap PAT secret '{secret_name}/{key}' after recreation attempt.",
        delay=20,
    )


# ---- Owner reference helpers ----------------------------------------------
def _owner_reference_for_instance(instance: Dict[str, Any]) -> Dict[str, Any]:
    metadata = instance.get("metadata", {})
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "GitLabInstance",
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "controller": False,
        "blockOwnerDeletion": True,
    }


def _ensure_instance_owner_reference(
    patch: Dict[str, Any], instance: Dict[str, Any]
) -> None:
    owner_ref = _owner_reference_for_instance(instance)
    if not owner_ref.get("name") or not owner_ref.get("uid"):
        raise kopf.TemporaryError(
            "GitLabInstance metadata is missing name/uid for ownerReference.", delay=10
        )
    metadata_patch = patch.setdefault("metadata", {})
    refs = metadata_patch.setdefault("ownerReferences", [])
    if any(ref.get("uid") == owner_ref["uid"] for ref in refs):
        return
    refs.append(owner_ref)


# ---- GitLab API helpers ----------------------------------------------------
def _gitlab_api(
    url: str, token: str, method: str, path: str, verify: bool = True, **kwargs: Any
) -> Any:
    try:
        req = requests.request(
            method=method,
            url=f"{url}{path}",
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
            timeout=30,
            verify=verify,
            **kwargs,
        )
    except requests.exceptions.SSLError as exc:
        hint = ""
        if verify:
            hint = (
                " (check the cert-manager Certificate resource status in the instance namespace, "
                "or set spec.insecureSkipTlsVerify: true for disposable environments)"
            )
        raise kopf.TemporaryError(
            f"TLS verification failed contacting GitLab{hint}: {exc}", delay=30
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise kopf.TemporaryError(
            f"GitLab API connection error: {exc}", delay=20
        ) from exc
    if req.status_code == 429:
        retry_after = int(req.headers.get("Retry-After", "60"))
        raise kopf.TemporaryError(
            f"GitLab API rate limited on {method} {path}. Retrying after {retry_after}s.",
            delay=retry_after,
        )
    if req.status_code == 404:
        return None
    if req.status_code == 401:
        raise kopf.TemporaryError(
            "GitLab API authentication failed (401 Unauthorized). "
            "Verify tokenSecretRef points to a valid GitLab Personal Access Token with `api` scope.",
            delay=20,
        )
    if req.status_code >= 400:
        raise kopf.TemporaryError(
            f"GitLab API {method} {path} failed: {req.status_code} {req.text}", delay=10
        )
    remaining = req.headers.get("RateLimit-Remaining")
    if remaining is not None and int(remaining) < 5:
        reset = int(req.headers.get("RateLimit-Reset", "0"))
        sleep_time = max(0, reset - int(time.time()))
        if sleep_time > 0:
            time.sleep(min(sleep_time, 10))
    if not req.text:
        return {}
    return req.json()


def _ensure_import_sources(
    gitlab_url: str, token: str, required: list[str], verify: bool
) -> None:
    settings = _gitlab_api(
        gitlab_url, token, "GET", "/api/v4/application/settings", verify=verify
    )
    existing = settings.get("import_sources", []) if isinstance(settings, dict) else []
    current = set(existing if isinstance(existing, list) else [])
    target = sorted(current.union(required))
    if target == sorted(current):
        return
    _gitlab_api(
        gitlab_url,
        token,
        "PUT",
        "/api/v4/application/settings",
        verify=verify,
        json={"import_sources": target},
    )


def _ensure_repository_for_user(
    *,
    gitlab_url: str,
    token: str,
    verify_tls: bool | str,
    username: str,
    repo_spec: Dict[str, Any],
) -> Dict[str, str]:
    project_path = repo_spec.get("projectPath", "workshop-repo")
    project_name = repo_spec.get("projectName", "workshop-repo")
    visibility = repo_spec.get("visibility", "private")
    import_url = repo_spec.get("importUrl")
    full_path = f"{username}/{project_path}"
    encoded = requests.utils.quote(full_path, safe="")

    existing_project = _gitlab_api(
        gitlab_url, token, "GET", f"/api/v4/projects/{encoded}", verify=verify_tls
    )
    if existing_project is None:
        namespaces = _gitlab_api(
            gitlab_url,
            token,
            "GET",
            "/api/v4/namespaces",
            verify=verify_tls,
            params={"search": username},
        )
        namespace_id = next(
            (
                ns["id"]
                for ns in namespaces
                if ns.get("full_path") == username or ns.get("path") == username
            ),
            None,
        )
        if namespace_id is None:
            raise kopf.TemporaryError(
                f"User namespace not found in GitLab for '{username}'", delay=10
            )

        payload = {
            "name": project_name,
            "path": project_path,
            "namespace_id": namespace_id,
            "visibility": visibility,
        }
        if import_url:
            # Explicit import requested: fail/retry if import cannot be performed.
            payload["import_url"] = import_url
            _ensure_import_sources(
                gitlab_url, token, ["git", "github"], verify=verify_tls
            )
            _gitlab_api(
                gitlab_url,
                token,
                "POST",
                "/api/v4/projects",
                verify=verify_tls,
                json=payload,
            )
        else:
            # No import URL requested: initialize an empty repository.
            payload["initialize_with_readme"] = True
            _gitlab_api(
                gitlab_url,
                token,
                "POST",
                "/api/v4/projects",
                verify=verify_tls,
                json=payload,
            )

    return {"fullPath": full_path, "webUrl": f"{gitlab_url}/{full_path}"}


def _lookup_gitlab_user(
    gitlab_url: str, token: str, username: str, verify_tls: bool | str
) -> Optional[Dict[str, Any]]:
    users = _gitlab_api(
        gitlab_url,
        token,
        "GET",
        "/api/v4/users",
        verify=verify_tls,
        params={"username": username},
    )
    if not isinstance(users, list):
        return None
    for user in users:
        if isinstance(user, dict) and user.get("username") == username:
            return user
    return None


def _delete_repository_for_user(
    *,
    gitlab_url: str,
    token: str,
    verify_tls: bool | str,
    username: str,
    repo_spec: Dict[str, Any],
) -> None:
    project_path = repo_spec.get("projectPath", "workshop-repo")
    full_path = f"{username}/{project_path}"
    encoded = requests.utils.quote(full_path, safe="")
    existing_project = _gitlab_api(
        gitlab_url, token, "GET", f"/api/v4/projects/{encoded}", verify=verify_tls
    )
    if existing_project is None:
        return
    project_id = (
        existing_project.get("id") if isinstance(existing_project, dict) else None
    )
    if project_id is None:
        return
    _gitlab_api(
        gitlab_url, token, "DELETE", f"/api/v4/projects/{project_id}", verify=verify_tls
    )


# ---- Status helpers --------------------------------------------------------
def _patch_status(
    namespace: str, plural: str, name: str, status: Dict[str, Any]
) -> None:
    """Immediately patch the status subresource so watchers see real-time progress."""
    api = kubernetes.client.CustomObjectsApi()
    api.patch_namespaced_custom_object_status(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=plural,
        name=name,
        body={"status": status},
    )


# ---- Kopf handlers ---------------------------------------------------------
@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    settings.posting.level = logging.INFO
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


@kopf.on.create(GROUP, VERSION, "gitlabinstances")
@kopf.on.update(GROUP, VERSION, "gitlabinstances")
def reconcile_instance(
    spec: Dict[str, Any],
    body: Dict[str, Any],
    namespace: str,
    patch: Dict[str, Any],
    **_: Any,
) -> None:
    instance_name = body.get("metadata", {}).get("name", "gitlab")

    # Immediately mark as in-progress so watchers see real-time status.
    _patch_status(
        namespace,
        "gitlabinstances",
        instance_name,
        {
            "ready": False,
            "message": "Reconciling GitLab instance.",
        },
    )

    # Ensure a cert-manager Certificate exists and its TLS secret is ready
    # before running the Helm install (the ingress depends on the TLS secret).
    tls_secret_name = _ensure_certificate(body, namespace)
    _ensure_tls_secret_ready(namespace, tls_secret_name)

    values_path = _instance_values_file(body)
    release = _instance_release_name(body)
    chart = _instance_chart(body)
    edition = _instance_edition(body)
    version = spec.get("chartVersion")
    cmd = [
        "helm",
        "upgrade",
        "--install",
        release,
        chart,
        "--namespace",
        namespace,
        "--set",
        f"global.edition={edition}",
        "-f",
        values_path,
        "--wait",
        "--timeout",
        "10m",
    ]
    if version:
        cmd.extend(["--version", version])

    try:
        try:
            _run(["helm", "repo", "add", "gitlab", "https://charts.gitlab.io/"])
        except subprocess.CalledProcessError:
            pass
        _run_or_retry(["helm", "repo", "update"], "helm repo update")
        _run_or_retry(cmd, "helm upgrade/install")
    finally:
        try:
            os.remove(values_path)
        except OSError:
            pass

    owner_ref = _owner_reference_for_instance(body)
    bootstrap_secret = _ensure_bootstrap_pat_secret(
        namespace,
        release,
        instance_name,
        owner_reference=owner_ref if owner_ref.get("uid") else None,
    )

    patch.setdefault("status", {})
    patch["status"]["ready"] = True
    patch["status"]["releaseName"] = release
    patch["status"]["gitlabUrl"] = _instance_gitlab_url(body)
    patch["status"]["bootstrapTokenSecret"] = bootstrap_secret
    patch["status"]["message"] = "Installed or upgraded successfully."
    kopf.event(
        body,
        type="Normal",
        reason="GitLabInstanceReconciled",
        message=f"Helm release reconciled: release={release}",
    )


@kopf.on.delete(GROUP, VERSION, "gitlabinstances", optional=True)
def delete_instance(body: Dict[str, Any], namespace: str, **_: Any) -> None:
    instance_name = body.get("metadata", {}).get("name")
    if not instance_name:
        raise kopf.PermanentError("GitLabInstance metadata.name is required.")

    # Ensure dependents are deleted before uninstalling Helm release so user
    # finalizers can still access instance context while it exists.
    _delete_users_for_instance(namespace, instance_name)
    remaining = _list_users_for_instance(namespace, instance_name)
    if remaining:
        raise kopf.TemporaryError(
            f"Waiting for {len(remaining)} dependent GitLabUser resource(s) to be deleted.",
            delay=5,
        )

    release = _instance_release_name(body)
    try:
        _run(["helm", "uninstall", release, "--namespace", namespace])
    except subprocess.CalledProcessError:
        pass
    kopf.event(
        body,
        type="Normal",
        reason="GitLabInstanceDeleted",
        message=f"Helm release delete attempted: release={release}",
    )


@kopf.on.create(GROUP, VERSION, "gitlabusers")
@kopf.on.update(GROUP, VERSION, "gitlabusers")
def reconcile_user(
    spec: Dict[str, Any],
    body: Dict[str, Any],
    namespace: str,
    patch: Dict[str, Any],
    **_: Any,
) -> None:
    username = body.get("metadata", {}).get("name")
    if not username:
        raise kopf.PermanentError("GitLabUser metadata.name is required.")

    # Immediately mark as in-progress so watchers see real-time status.
    _patch_status(
        namespace,
        "gitlabusers",
        username,
        {
            "ready": False,
            "message": "Accepted. Reconciling GitLab user.",
        },
    )

    instance_name, instance_namespace = _resolve_instance_ref(
        spec["instanceRef"], namespace
    )
    instance = _get_instance(instance_namespace, instance_name)
    # Owner references are only valid within the same namespace; skip when the
    # GitLabInstance lives in a different namespace to avoid the GC deleting the user.
    if instance_namespace == namespace:
        _ensure_instance_owner_reference(patch, instance)

    # Gate on instance readiness to avoid hammering a not-yet-ready GitLab.
    if not instance.get("status", {}).get("ready"):
        raise kopf.TemporaryError(
            f"GitLabInstance '{instance_namespace}/{instance_name}' is not ready yet.",
            delay=15,
        )

    # External URL is stored in status for user-facing access.
    gitlab_url = instance.get("status", {}).get("gitlabUrl") or _instance_gitlab_url(
        instance
    )
    # Internal cluster URL used for all API calls — plain HTTP, no TLS.
    api_url = _instance_internal_url(instance, instance_namespace)

    token = _instance_pat_token(
        namespace=instance_namespace,
        instance=instance,
        token_secret_ref=spec.get("tokenSecretRef"),
    )

    repositories = spec.get("repositories", [])
    if repositories is None:
        repositories = []
    if not isinstance(repositories, list):
        raise kopf.PermanentError(
            "GitLabUser spec.repositories must be a list when provided."
        )

    existing = _gitlab_api(
        api_url,
        token,
        "GET",
        "/api/v4/users",
        verify=False,
        params={"username": username},
    )
    if not existing:
        payload = {
            "email": spec.get("email", f"{username}@educates.test"),
            "username": username,
            "name": spec.get("name", username),
            "password": spec["password"],
            "skip_confirmation": True,
            "admin": spec.get("admin", False),
        }
        _gitlab_api(
            api_url,
            token,
            "POST",
            "/api/v4/users",
            verify=False,
            json=payload,
        )
    else:
        # Update existing user to reflect spec changes (password, email, admin, etc.).
        existing_user = existing[0] if isinstance(existing, list) else existing
        if isinstance(existing_user, dict) and existing_user.get("id"):
            update_payload = {
                "email": spec.get("email", f"{username}@educates.test"),
                "name": spec.get("name", username),
                "password": spec["password"],
                "admin": spec.get("admin", False),
            }
            _gitlab_api(
                api_url,
                token,
                "PUT",
                f"/api/v4/users/{existing_user['id']}",
                verify=False,
                json=update_payload,
            )

    repo_results = []
    for repo in repositories:
        if not isinstance(repo, dict):
            raise kopf.PermanentError(
                "Each item in GitLabUser spec.repositories must be an object."
            )
        repo_results.append(
            _ensure_repository_for_user(
                gitlab_url=api_url,
                token=token,
                verify_tls=False,
                username=username,
                repo_spec=repo,
            )
        )

    patch.setdefault("status", {})
    patch["status"]["ready"] = True
    patch["status"]["username"] = username
    patch["status"]["gitlabUrl"] = gitlab_url
    patch["status"]["message"] = "User reconciled successfully."
    if repositories:
        patch["status"]["repositories"] = repo_results
    kopf.event(
        body,
        type="Normal",
        reason="GitLabUserReconciled",
        message=f"User reconciled: username={username}, repositories_processed={len(repositories)}",
    )


@kopf.on.delete(GROUP, VERSION, "gitlabusers", optional=True)
def delete_user(
    spec: Dict[str, Any], body: Dict[str, Any], namespace: str, **_: Any
) -> None:
    instance = None
    # Fall back to the URL cached in status if the instance is already gone.
    api_url = body.get("status", {}).get("gitlabUrl")
    instance_name, instance_namespace = _resolve_instance_ref(
        spec.get("instanceRef", {}), namespace
    )
    try:
        instance = _get_instance(instance_namespace, instance_name)
        api_url = _instance_internal_url(instance, instance_namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        kopf.event(
            body,
            type="Warning",
            reason="InstanceNotFoundOnUserDelete",
            message=f"GitLabInstance '{instance_namespace}/{instance_name}' not found; proceeding with best-effort user cleanup.",
        )

    # Resolve PAT token — same best-effort approach.
    token = None
    try:
        if instance is not None:
            token = _instance_pat_token(
                namespace=instance_namespace,
                instance=instance,
                token_secret_ref=spec.get("tokenSecretRef"),
            )
        else:
            ref = (
                spec.get("tokenSecretRef")
                if isinstance(spec.get("tokenSecretRef"), dict)
                else {}
            )
            secret_name = ref.get(
                "name",
                _instance_bootstrap_pat_secret_name(instance_name or "gitlab"),
            )
            key = ref.get("key", DEFAULT_BOOTSTRAP_PAT_KEY)
            token = _secret_value(namespace, secret_name, key).strip()
    except kopf.TemporaryError:
        token = None
    username = body.get("metadata", {}).get("name")
    if not username:
        raise kopf.PermanentError("GitLabUser metadata.name is required.")
    repositories = spec.get("repositories", [])
    if repositories is None:
        repositories = []

    deleted_repos = 0
    deleted_user = False
    if not api_url or not token:
        kopf.event(
            body,
            type="Warning",
            reason="GitLabCleanupSkipped",
            message="Skipping remote GitLab cleanup because URL or token is unavailable; allowing Kubernetes resource deletion.",
        )
        return

    for repo in repositories:
        if isinstance(repo, dict):
            _delete_repository_for_user(
                gitlab_url=api_url,
                token=token,
                verify_tls=False,
                username=username,
                repo_spec=repo,
            )
            deleted_repos += 1

    user = _lookup_gitlab_user(api_url, token, username, False)
    if user and user.get("id") is not None:
        _gitlab_api(
            api_url,
            token,
            "DELETE",
            f"/api/v4/users/{user['id']}",
            verify=False,
        )
        deleted_user = True
    kopf.event(
        body,
        type="Normal",
        reason="GitLabCleanup",
        message=(
            f"Remote cleanup completed: repositories_processed={deleted_repos}, "
            f"user_deleted={str(deleted_user).lower()}, username={username}"
        ),
    )


# ---- Liveness probe -------------------------------------------------------
@kopf.on.probe(id="health")
def health_probe(**_: Any) -> str:
    return "ok"
