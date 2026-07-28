{{/* Expand the name of the chart. */}}
{{- define "truenas-scale-exporter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Fully qualified app name. */}}
{{- define "truenas-scale-exporter.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "truenas-scale-exporter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "truenas-scale-exporter.labels" -}}
helm.sh/chart: {{ include "truenas-scale-exporter.chart" . }}
{{ include "truenas-scale-exporter.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "truenas-scale-exporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "truenas-scale-exporter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "truenas-scale-exporter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "truenas-scale-exporter.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding the API key: either one the user already created,
or the one this chart renders.
*/}}
{{- define "truenas-scale-exporter.secretName" -}}
{{- if .Values.truenas.existingSecret }}
{{- .Values.truenas.existingSecret }}
{{- else }}
{{- include "truenas-scale-exporter.fullname" . }}
{{- end }}
{{- end }}

{{- define "truenas-scale-exporter.secretKey" -}}
{{- if .Values.truenas.existingSecret }}
{{- .Values.truenas.existingSecretKey }}
{{- else }}
{{- "api-key" }}
{{- end }}
{{- end }}

{{/*
Fail early and clearly rather than deploying a pod that will crash-loop on a
missing URL or key — the error a user gets from a CrashLoopBackOff is far less
actionable than this one.
*/}}
{{- define "truenas-scale-exporter.validate" -}}
{{- if not .Values.truenas.url }}
{{- fail "truenas.url is required — set it to your TrueNAS host, e.g. --set truenas.url=truenas.example.com" }}
{{- end }}
{{- if and (not .Values.truenas.apiKey) (not .Values.truenas.existingSecret) }}
{{- fail "Provide an API key: set truenas.existingSecret (preferred) or truenas.apiKey" }}
{{- end }}
{{- end }}
