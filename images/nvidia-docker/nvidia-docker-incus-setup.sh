#!/usr/bin/env bash

set -euo pipefail

# Incus/libnvidia-container exposes the assigned /dev/nvidiaN devices and
# driver libraries, but some combinations don't recreate the matching procfs
# GPU entries. The nested NVIDIA runtime stats these paths before starting an
# OCI container. Recreate entries only for device nodes actually assigned to
# this Incus container; unassigned host GPUs are intentionally ignored.
if ! command -v nvidia-smi >/dev/null 2>&1; then
  exit 0
fi

proc_gpu_dir=/proc/driver/nvidia/gpus
mkdir -p "$proc_gpu_dir"

pci_address=""
while IFS= read -r line; do
  case "$line" in
    *'<gpu id="'*)
      pci_address=${line#*<gpu id=\"}
      pci_address=${pci_address%%\"*}
      pci_address=${pci_address/00000000:/0000:}
      pci_address=${pci_address,,}
      ;;
    *'<minor_number>'*)
      minor_number=${line#*<minor_number>}
      minor_number=${minor_number%%</minor_number>*}
      device=/dev/nvidia${minor_number}

      if [[ "$minor_number" =~ ^[0-9]+$ && "$pci_address" =~ ^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7]$ && -e "$device" ]]; then
        ln -sfn "$device" "$proc_gpu_dir/$pci_address"
      fi
      pci_address=""
      ;;
  esac
done < <(nvidia-smi -q -x)
