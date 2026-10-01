# Talos Patching

These are Talos strategic merge patches loaded by [topf](https://postfinance.github.io/topf/main/configuration-model/).
The active inventory is [`../topf.yaml`](../topf.yaml), which sets `patchesDir: patches`.

## Merge order

Patches merge alphabetically within each directory, in this order:

- `all/`: every node
- `control-plane/` or `worker/`: nodes with that role
- `node/<hostname>/`: one specific node

Files ending in `.yaml.tpl` are Go templates with `.Node` and `.Data` supplied by topf.
Use `$patch: delete` directly; there is no talhelper environment substitution to escape.
The loose `cluster.yaml.j2` draft is outside these directories and is not loaded.

## Network and storage

`all/20-network-links.yaml.tpl` assigns the configured MAC an alias (`net0`) and
configures its static address, MTU and default route with `LinkConfig`. An alias
does not rename the kernel interface or create a bond. No DHCP document is added.
`control-plane/20-vip.yaml.tpl` attaches the API VIP only to control-plane nodes.
DNS and NTP use `ResolverConfig` and `TimeSyncConfig`.

The configuration targets the existing Talos **1.14.0** pin. It uses the typed
network documents introduced in earlier Talos releases, plus 1.14 documents for
installation, Kubernetes components, sysctls, files and kernel modules. It is not
a configuration for booting Talos 1.12 installation media.

`all/30-kubelet.yaml.tpl` deliberately retains the supported `machine.kubelet`
section and deletes the generated `KubeletConfig` document: the latter has no
`extraMounts` field in Talos 1.14. The bind mount at `/var/openebs/local` is still
needed by the existing OpenEBS configuration. `KubeNodeConfig` supplies node IPs
and labels separately. Migrating this last kubelet section requires a separate
storage/data migration; removing the mount alone would break existing volumes.
`SecurityProfileConfig.workloadIsolation: false` preserves the previous workload
isolation behavior.
