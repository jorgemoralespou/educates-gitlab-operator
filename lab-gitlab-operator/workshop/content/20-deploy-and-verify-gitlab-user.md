---
title: Create a GitLab User
---

## Create a GitLabUser

Now that your GitLab server is running, create a user with a bootstrapped repository.

```terminal:execute
terminal: 1
command: |-
  cat > ~/gitlabuser.yaml <<'EOF'
  apiVersion: gitlab.operators.educates.dev/v1beta1
  kind: GitLabUser
  metadata:
    name: {{< param session_namespace >}}
    namespace: {{< param session_namespace >}}
  spec:
    instanceRef:
      name: {{< param session_namespace >}}
    password: WorkshopPass123!
    email: student@example.com
    name: Workshop Student
    repositories:
      - projectName: demo
        projectPath: demo
        visibility: private
  EOF
```

The `instanceRef` links this user to the GitLab instance you created in the previous step. The operator will:

1. Wait for the instance to be ready.
2. Create the user via the GitLab REST API.
3. Create a `demo` repository in the user's namespace.

Apply it:

```terminal:execute
terminal: 1
command: kubectl apply -f ~/gitlabuser.yaml
```

## Watch the user reconciliation

```terminal:execute
terminal: 1
command: watch kubectl get gitlabuser {{< param session_namespace >}}
```

Wait until `READY` shows `True`, then press `Ctrl+C`.

## Inspect the results

Check the status of both resources:

```terminal:execute
terminal: 1
command: |-
  kubectl get gitlabinstances
  kubectl get gitlabusers
```

Look at the user details and events:

```terminal:execute
terminal: 1
command: kubectl describe gitlabuser {{< param session_namespace >}}
```

## Access GitLab

Your GitLab instance is available at the URL shown in the instance status. You can log in with:

- **Username:** `{{< param session_namespace >}}`
- **Password:** `WorkshopPass123!`

```terminal:execute
terminal: 1
command: kubectl get gitlabinstance {{< param session_namespace >}} -o jsonpath='{.status.gitlabUrl}{"\n"}'
```
