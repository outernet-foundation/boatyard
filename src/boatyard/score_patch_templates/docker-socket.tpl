# Docker socket bind for workloads declaring boatyard/docker-socket —
# container-controllers that start and stop sibling services through the
# host daemon (score's workload spec carries no volume grammar, so the bind
# rides this compose patch the same way device reservations ride the gpu
# templates). The socket path is the one compose itself defaults to; hosts
# with a different daemon socket spell an absolute-source bind in a
# provisioner service instead. No k8s twin: a workload held out of the k8s
# list never reaches that rendering, and cluster workloads take a
# service-account scoped Docker API, not a node socket.
{{- $matched := false }}
{{- range $workloadName, $workload := .Workloads }}
{{- if eq (dig "metadata" "annotations" "boatyard/docker-socket" "" $workload) "true" }}
{{- range $containerName, $_ := $workload.containers }}
{{- $matched = true }}
- op: set
  path: services.{{ $workloadName }}-{{ $containerName }}.volumes
  value:
    - type: bind
      source: /var/run/docker.sock
      target: /var/run/docker.sock
  description: Docker socket bind for {{ $workloadName }}-{{ $containerName }}
{{- end }}
{{- end }}
{{- end }}
{{- if not $matched }}
[]
{{- end }}
