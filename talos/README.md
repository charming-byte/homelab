# Talos configuration

This directory contains the Talos Linux configuration for the cluster. topf renders
the machine configuration for each node and applies it to the nodes.

## Cluster

| Node   | Role          | Address           | MAC               | Install disk |
| ------ | ------------- | ----------------- | ----------------- | ------------ |
| node01 | worker        | 192.168.178.55/24 | e8:6a:64:f4:8b:6d | /dev/sda     |
| node02 | control-plane | 192.168.178.56/24 | c8:d3:ff:9b:ff:36 | /dev/sda     |

The Kubernetes API endpoint is `https://192.168.178.200:6443`. node02 holds this
virtual IP. The gateway is `192.168.178.1` and the MTU is 1500.

`topf.yaml` sets the Talos version and the Kubernetes version. Renovate updates
both versions.

## Directory layout

| Path                  | Content                                                              |
| --------------------- | -------------------------------------------------------------------- |
| `topf.yaml`           | Nodes, Talos and Kubernetes versions, values for the patch templates |
| `talsecret.sops.yaml` | Cluster secrets bundle, encrypted with SOPS                          |
| `patches/`            | Machine configuration patches ([README](patches/README.md))          |
| `schematics/`         | Image Factory schematic for each node                                |
| `mod.just`            | Recipes for the `just talos` module                                  |
| `rendered/`           | Rendered machine configurations (generated, ignored by Git)          |
| `clusterconfig/`      | `talosconfig` for `talosctl` (generated, ignored by Git)             |

The files in `rendered/` and `clusterconfig/` contain secrets. Do not commit them.

The secrets bundle defines the identity of the cluster. Keep `talsecret.sops.yaml`
and the age key safe. Do not generate a new secrets bundle for this cluster.

## Installation images

Each node installs Talos from an Image Factory image. The schematic in
`schematics/<hostname>.yaml` sets the system extensions and kernel arguments of the
image for that node. The machine configuration installs Talos on `/dev/sda` and does
not wipe the disk.

## Prerequisites

Run all commands from the repository root. The commands need the following:

- The tools that mise installs.
- The age key in `age.key`. mise sets `SOPS_AGE_KEY_FILE` to this file.
- For commands that connect to the nodes: `talos/clusterconfig/talosconfig`. mise
  sets `TALOSCONFIG` to this file.

## Recipes

The recipes select a node by its hostname, for example `node01`.

| Recipe                                | Action                                                         |
| ------------------------------------- | -------------------------------------------------------------- |
| `just talos render`                   | Render the machine configurations to `talos/rendered/`.        |
| `just talos diff`                     | Show pending configuration changes. Does not change the nodes. |
| `just talos apply`                    | Apply the configuration to all nodes.                          |
| `just talos apply-node <node> [mode]` | Apply the configuration to one node.                           |
| `just talos upgrade`                  | Upgrade Talos on all nodes, one node at a time.                |
| `just talos upgrade-node <node>`      | Upgrade Talos on one node.                                     |
| `just talos upgrade-k8s`              | Upgrade Kubernetes with `talosctl`.                            |
| `just talos nodes`                    | List all nodes and their current state.                        |
| `just talos reboot-node <node>`       | Reboot one node.                                               |
| `just talos reset-node <node>`        | Reset one node to maintenance mode. **Destructive.**           |
| `just talos reset`                    | Reset all nodes to maintenance mode. **Destructive.**          |

The apply and upgrade recipes show the changes and ask for confirmation first.
`apply-node` accepts these modes: `auto` (default), `reboot`, `no-reboot`, `staged`
and `try`.

`just bootstrap talos` creates the cluster from nodes in maintenance mode. This
recipe applies the configuration, bootstraps etcd and writes
`talos/clusterconfig/talosconfig` and `kubeconfig`.
