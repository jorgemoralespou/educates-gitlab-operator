---
title: Workshop Summary
---

You explored the core operator behaviors:

- **`GitLabInstance` reconciliation** — a single CR deployed a full GitLab server via Helm.
- **Automatic PAT bootstrap** — the operator generated a root token and stored it in a Kubernetes secret.
- **`GitLabUser` creation** — the operator created a user and repository via the GitLab REST API.
- **Event visibility** — you tracked reconciliation progress through `kubectl describe` events.

All of this was driven by two simple YAML manifests with no TLS or Helm configuration — the operator handled the complexity.

## Cleanup

Deleting your session will automatically clean up all resources. If you want to clean up manually:

```terminal:execute
terminal: 1
command: |-
  kubectl delete -f ~/gitlabuser.yaml --ignore-not-found
  kubectl delete -f ~/gitlabinstance.yaml --ignore-not-found
```
