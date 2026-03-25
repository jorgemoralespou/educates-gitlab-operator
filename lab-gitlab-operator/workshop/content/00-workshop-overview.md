---
title: Workshop Overview
---

In this lab you will explore the GitLab operator by creating your own GitLab server and user, all managed through Kubernetes custom resources.

You will:

1. Verify that the GitLab operator is running in the cluster.
2. Create a `GitLabInstance` to deploy a GitLab server in your session namespace.
3. Create a `GitLabUser` with a bootstrapped repository.
4. Inspect readiness, status, and Kubernetes events to follow the reconciliation flow.

{{< note >}}
The operator is pre-installed in this cluster. TLS and CA certificates are configured at the operator level, so you don't need to worry about them.
{{< /note >}}

Continue to the next page to get started.
