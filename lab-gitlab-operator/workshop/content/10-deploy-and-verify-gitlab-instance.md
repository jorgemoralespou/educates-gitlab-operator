---
title: Deploy a GitLab Server
---

## Create a GitLabInstance

Create a `GitLabInstance` manifest. The operator will use Helm to install a full GitLab server in your session namespace.

```terminal:execute
terminal: 1
command: |-
  cat > ~/gitlabinstance.yaml <<'EOF'
  apiVersion: gitlab.operators.educates.dev/v1beta1
  kind: GitLabInstance
  metadata:
    name: {{< param session_namespace >}}
    namespace: {{< param session_namespace >}}
  spec:
    domain: {{< param ingress_domain >}}
  EOF
```

Notice how simple the manifest is — just a name and a domain. TLS certificates are configured at the operator level, so you don't need to specify them here.

Apply it to the cluster:

```terminal:execute
terminal: 1
command: kubectl apply -f ~/gitlabinstance.yaml
```

## Watch the instance come up

The operator will install GitLab via Helm and wait for all pods to be ready. This takes a few minutes.

```terminal:execute
terminal: 1
command: watch kubectl get gitlabinstance {{< param session_namespace >}}
```

Wait until the `READY` column shows `True`, then press `Ctrl+C` to stop the watch.

{{< note >}}
While waiting, you can open a second terminal and run `kubectl get pods` to watch the GitLab pods starting up.
{{< /note >}}

## Inspect the instance details

Once the instance is ready, look at the status and events:

```terminal:execute
terminal: 1
command: kubectl describe gitlabinstance {{< param session_namespace >}}
```

Notice the status fields: `gitlabUrl`, `releaseName`, `bootstrapTokenSecret`, and the `GitLabInstanceReconciled` event.
