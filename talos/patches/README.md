# Talos patches

This directory contains the patches that topf merges into the Talos machine
configuration of each node. `../topf.yaml` sets `patchesDir: patches`.

| Directory        | Nodes that get the patch |
| ---------------- | ------------------------ |
| `all/`           | All nodes                |
| `control-plane/` | node02                   |

topf merges `all/` first and `control-plane/` second. In each directory, topf
merges the files in alphabetical order. Files that end in `.yaml.tpl` are templates.
They get their values from `topf.yaml`.

## Patches for all nodes

| File                        | Configuration                                              |
| --------------------------- | ---------------------------------------------------------- |
| `00-install.yaml.tpl`       | Install disk from `installDisk`, without wiping the disk   |
| `01-hostname.yaml.tpl`      | Hostname of the node                                       |
| `10-cluster.yaml`           | Certificate SANs, pod and service subnets; removes Flannel |
| `20-network-links.yaml.tpl` | Network interface, static address, MTU and default route   |
| `21-network.yaml`           | DNS                                                        |
| `22-time.yaml`              | NTP                                                        |
| `30-kubelet.yaml.tpl`       | Kubelet, node IP subnet and node labels                    |
| `40-sysctls.yaml`           | Kernel parameters                                          |
| `50-files.yaml`             | containerd settings and NFS mount options                  |
| `61-kernel-modules.yaml`    | Kernel modules                                             |
| `70-security.yaml`          | Workload isolation                                         |

## Patches for the control-plane node

| File                       | Configuration                                              |
| -------------------------- | ---------------------------------------------------------- |
| `00-cluster.yaml.tpl`      | Control-plane components, scheduling and admission control |
| `10-talos-api-access.yaml` | Talos API access for selected Kubernetes namespaces        |
| `20-vip.yaml.tpl`          | Virtual IP of the Kubernetes API on `net0`                 |

## Cluster

The pod subnet is `10.42.0.0/16` and the service subnet is `10.43.0.0/16`. Cilium
is the CNI and replaces kube-proxy. For this reason, `10-cluster.yaml` removes
Flannel and `00-cluster.yaml.tpl` disables kube-proxy. `00-cluster.yaml.tpl` also
disables the CoreDNS of Talos, because CoreDNS is managed in the cluster.

On node02, `00-cluster.yaml.tpl` removes the control-plane taint, so that workloads
can run on node02. The same patch removes the `PodSecurity` admission
configuration of Talos. etcd serves metrics on port 2381 on all addresses. The
controller manager and the scheduler bind to all addresses.

`10-talos-api-access.yaml` gives the `os:admin` role for the Talos API to these
namespaces: `dbms`, `actions-runner-system`, `system-upgrade` and `network`.

## Network

Each node uses one network interface. `20-network-links.yaml.tpl` selects this
interface by the MAC address in `topf.yaml` and gives it the alias `net0`. The alias
does not rename the kernel interface. The nodes use static addresses and no DHCP.

## Kubelet

The kubelet bind-mounts `/var/openebs/local` from the host. OpenEBS Local PV stores
its volumes in this directory. The typed `KubeletConfig` document has no field for
extra mounts. For this reason, `30-kubelet.yaml.tpl` configures the kubelet in the
`machine.kubelet` section and deletes the generated `KubeletConfig` document.
A `KubeNodeConfig` document sets the node IP subnet and the node labels.

## Workload isolation

`70-security.yaml` disables workload isolation. The storage and GPU workloads need
access to the host.
