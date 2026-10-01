---
# Talos 1.14's typed KubeletConfig has no extraMounts equivalent. Keep the
# supported legacy kubelet section until OpenEBS data is migrated separately.
machine:
  kubelet:
    image: ghcr.io/siderolabs/kubelet:{{ .KubernetesVersion }}
    extraConfig:
      serializeImagePulls: false
    defaultRuntimeSeccompProfileEnabled: true
    disableManifestsDirectory: true
    extraMounts:
      - destination: /var/openebs/local
        type: bind
        source: /var/openebs/local
        options:
          - bind
          - rshared
          - rw
---
apiVersion: v1alpha1
kind: KubeletConfig
$patch: delete
---
apiVersion: v1alpha1
kind: KubeNodeConfig
nodeIP:
  validSubnets:
    - "{{ .Data.nodeSubnet }}"
labels:
  node.kubernetes.io/gpu: "true"
