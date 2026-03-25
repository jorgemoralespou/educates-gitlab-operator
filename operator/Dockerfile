FROM fedora:42

RUN INSTALL_PKGS=" \
        curl \
        findutils \
        glibc-langpack-en \
        procps \
        tar \
        which \
        uv \
    " && \
    dnf install -y --setopt=tsflags=nodocs $INSTALL_PKGS && \
    dnf clean -y --enablerepo='*' all && \
    useradd -u 1001 -g 0 -M -d /opt/app-root/src default && \
    mkdir -p /opt/app-root/src && \
    chown -R 1001:0 /opt/app-root

WORKDIR /opt/app-root

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANG=en_US.UTF-8

# Helm is needed by the GitLabInstance reconciler.
ENV HELM_VERSION="v4.1.1"
RUN set -eux; \
    arch="$(uname -m)"; \
    case "${arch}" in \
      x86_64) helm_arch="amd64" ;; \
      aarch64|arm64) helm_arch="arm64" ;; \
      *) echo "Unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz"; \
    tar -xzf /tmp/helm.tgz -C /tmp; \
    install -m 0755 "/tmp/linux-${helm_arch}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tgz "/tmp/linux-${helm_arch}"

USER 1001

RUN uv python install

COPY --chown=1001:0 pyproject.toml uv.lock /opt/app-root/

RUN uv sync --frozen --no-dev

COPY --chown=1001:0 ./ /opt/app-root

ENV PATH="/opt/app-root/.venv/bin:$PATH"

CMD [ "python", "-m", "kopf", "run", "--standalone", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "main.py" ]
