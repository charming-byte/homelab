#!/usr/bin/env bash
set -euo pipefail

# Run from talos/. Render into a fresh private directory so validation cannot
# accidentally accept stale output from an earlier successful generation.
umask 077
test -f topf.yaml
test -f talsecret.sops.yaml
work_dir=$(mktemp -d ./rendered-check.XXXXXX)
trap 'rm -rf -- "$work_dir"' EXIT

topf render --output "$work_dir"
node_names=$(yq -er '.nodes[].host' topf.yaml)
nodes=()
while IFS= read -r node; do
    nodes+=("$node")
done <<< "$node_names"
for node in "${nodes[@]}"; do
    talosctl validate --config "$work_dir/$node.yaml" --mode metal --strict
done
topf talosconfig > "$work_dir/talosconfig"

mkdir -p rendered clusterconfig
for node in "${nodes[@]}"; do
    install -m 600 "$work_dir/$node.yaml" "rendered/$node.yaml"
done
install -m 600 "$work_dir/talosconfig" clusterconfig/talosconfig
printf 'Validated %s node configs in talos/rendered; updated talos/clusterconfig/talosconfig.\n' "${#nodes[@]}"
