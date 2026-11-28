{{- define "local-llm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "local-llm.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "local-llm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "local-llm.labels" -}}
app.kubernetes.io/name: {{ include "local-llm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "local-llm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "local-llm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "local-llm.image" -}}
{{- if eq .Values.image.flavor "cuda" -}}
{{- .Values.image.cudaDigest -}}
{{- else if eq .Values.image.flavor "cpu" -}}
{{- .Values.image.cpuDigest -}}
{{- else -}}
{{- fail "image.flavor must be 'cpu' or 'cuda'" -}}
{{- end -}}
{{- end -}}
