# GitLab Operator Showcase

Educates workshop that demonstrates the GitLab Workshop Operator. Learners create a `GitLabInstance` and `GitLabUser` custom resource and observe the operator reconciliation flow.

## Prerequisites

- The GitLab Workshop Operator must be pre-installed in the cluster.
- Cluster ingress and TLS must be configured.

## Deploy

```bash
kubectl apply -f resources/workshop.yaml
kubectl apply -f resources/trainingportal.yaml
```
