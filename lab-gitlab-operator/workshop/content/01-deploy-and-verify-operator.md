---
title: Verify the Operator
---

The GitLab operator is pre-installed in this cluster. Let's verify it is running before we start creating resources.

## Check operator status

```terminal:execute
terminal: 1
command: kubectl get pods -l app=educates-gitlab-operator --all-namespaces
```

You should see the operator pod in a `Running` state.

## Verify the CRDs are installed

```terminal:execute
terminal: 1
command: kubectl api-resources --api-group=gitlab.operators.educates.dev
```

You should see two resources listed: `gitlabinstances` and `gitlabusers`.

Now that you've confirmed the operator is ready, let's create a GitLab instance.
