---
# Select the existing NIC without adding a bond or changing its kernel name.
apiVersion: v1alpha1
kind: LinkAliasConfig
name: net0
selector:
  match: mac(link.hardware_addr) == "{{ .Node.Data.macAddr }}"
---
apiVersion: v1alpha1
kind: LinkConfig
name: net0
mtu: {{ .Node.Data.mtu }}
addresses:
  - address: "{{ .Node.IP }}/{{ .Data.prefixLength }}"
routes:
  # An omitted destination creates the default route for the gateway family.
  - gateway: "{{ .Data.gateway }}"
