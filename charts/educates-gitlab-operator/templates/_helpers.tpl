{{- define "educates-gitlab-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "educates-gitlab-operator.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "educates-gitlab-operator.name" . -}}
{{- end -}}
{{- end -}}

{{- define "educates-gitlab-operator.namespace" -}}
{{- default .Release.Namespace .Values.namespace.name -}}
{{- end -}}
