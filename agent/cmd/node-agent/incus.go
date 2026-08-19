package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

func safeDeviceName(prefix string, values ...any) string {
	raw := fmt.Sprintf("%s-%s", prefix, strings.Trim(strings.ReplaceAll(fmt.Sprint(values...), " ", "-"), "-"))
	raw = strings.ToLower(raw)
	var builder strings.Builder
	for _, r := range raw {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' {
			builder.WriteRune(r)
		}
	}
	name := strings.Trim(builder.String(), "-")
	if name == "" {
		return prefix
	}
	if len(name) > 48 {
		return name[:48]
	}
	return name
}

func shellSingleQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func authorizedKeys(keys ...string) string {
	seen := map[string]bool{}
	lines := []string{}
	for _, value := range keys {
		for _, line := range strings.Split(value, "\n") {
			line = strings.TrimSpace(line)
			if line != "" && !seen[line] {
				seen[line] = true
				lines = append(lines, line)
			}
		}
	}
	return strings.Join(lines, "\n")
}

func isContainerHomePath(path string, username string) bool {
	username = strings.TrimSpace(username)
	if username == "" {
		username = "ubuntu"
	}
	return filepath.Clean(path) == filepath.Clean("/home/"+username)
}

func cloudInitUserData(username string, sshKeys ...string) string {
	if username == "" {
		username = "ubuntu"
	}
	var builder strings.Builder
	builder.WriteString("#cloud-config\n")
	builder.WriteString("users:\n")
	builder.WriteString(fmt.Sprintf("  - name: %s\n", username))
	builder.WriteString("    uid: 1000\n")
	builder.WriteString("    groups: sudo\n")
	builder.WriteString("    shell: /bin/bash\n")
	builder.WriteString("    sudo: ALL=(ALL) NOPASSWD:ALL\n")
	builder.WriteString("    lock_passwd: true\n")
	sshKey := authorizedKeys(sshKeys...)
	if sshKey != "" {
		builder.WriteString("    ssh_authorized_keys:\n")
		for _, line := range strings.Split(sshKey, "\n") {
			line = strings.TrimSpace(line)
			if line != "" {
				builder.WriteString(fmt.Sprintf("      - %s\n", line))
			}
		}
	}
	builder.WriteString("ssh_pwauth: false\n")
	return builder.String()
}

func homeSkeletonSeedScript() string {
	return `set -eu
home_dir=$1
skel_dir=${2:-/etc/skel}

if [ ! -d "$skel_dir" ]; then
  exit 0
fi

# The shared home may already contain user data from another container. Copy
# only missing top-level entries so image defaults are added without replacing
# anything the user has created or changed.
for source in "$skel_dir"/* "$skel_dir"/.[!.]* "$skel_dir"/..?*; do
  if [ ! -e "$source" ] && [ ! -L "$source" ]; then
    continue
  fi
  name=${source##*/}
  target=$home_dir/$name
  if [ -e "$target" ] || [ -L "$target" ]; then
    continue
  fi
  # Concurrent starts for the same shared home are harmless: another
  # container may win the copy between the existence check and cp.
  cp -R -- "$source" "$target" 2>/dev/null || [ -e "$target" ] || [ -L "$target" ]
done
`
}

func initializeContainerSSHWithMounts(container string, username string, mounts []string, sshKeys ...string) error {
	username = strings.TrimSpace(username)
	if username == "" {
		username = "ubuntu"
	}
	sshKey := authorizedKeys(sshKeys...)
	if sshKey == "" {
		return fmt.Errorf("ssh public key is empty")
	}
	encodedKey := base64.StdEncoding.EncodeToString([]byte(sshKey))
	encodedSkeletonSeed := base64.StdEncoding.EncodeToString([]byte(homeSkeletonSeedScript()))
	script := fmt.Sprintf(`
set -eu
user=%s
key_b64=%s
skel_seed_b64=%s

if ! id "$user" >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd -u 1000 -m -s /bin/bash "$user"
  elif command -v adduser >/dev/null 2>&1; then
    adduser -D -u 1000 -s /bin/sh "$user"
  fi
fi

if ! id "$user" >/dev/null 2>&1 || [ "$(id -u "$user")" != "1000" ] || [ "$(id -g "$user")" != "1000" ]; then
  echo "platform SSH user must have UID/GID 1000:1000" >&2
  exit 1
fi

if command -v usermod >/dev/null 2>&1; then
  usermod -aG sudo "$user" 2>/dev/null || usermod -aG wheel "$user" 2>/dev/null || true
  if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$user"
  fi
fi

home_dir=$(getent passwd "$user" 2>/dev/null | cut -d: -f6 || true)
if [ -z "$home_dir" ]; then
  home_dir="/home/$user"
  mkdir -p "$home_dir"
fi
chown "$user:$user" "$home_dir" 2>/dev/null || chown "$user" "$home_dir" 2>/dev/null || true

# A bind-mounted NFS home is commonly owned by the mapped UID (for example
# host UID 1001000), so container root cannot safely populate it. Run the
# skeleton copy as the platform user (UID/GID 1000:1000).
skel_seed_file=$(mktemp)
printf '%%s' "$skel_seed_b64" | base64 -d > "$skel_seed_file"
chmod 0755 "$skel_seed_file"
seed_ok=0
if command -v runuser >/dev/null 2>&1; then
  runuser -u "$user" -- sh "$skel_seed_file" "$home_dir" && seed_ok=1
elif command -v su >/dev/null 2>&1; then
  su -s /bin/sh "$user" -c 'sh "$1" "$2"' sh "$skel_seed_file" "$home_dir" && seed_ok=1
elif command -v setpriv >/dev/null 2>&1; then
  setpriv --reuid=1000 --regid=1000 --clear-groups sh "$skel_seed_file" "$home_dir" && seed_ok=1
fi
rm -f "$skel_seed_file"
if [ "$seed_ok" -ne 1 ]; then
  echo "failed to seed missing files from /etc/skel into $home_dir as $user" >&2
  exit 1
fi
if [ -d /workspace ]; then
  chmod 1777 /workspace 2>/dev/null || chmod 0777 /workspace 2>/dev/null || true
fi

key_file=$(mktemp)
printf '%%s' "$key_b64" | base64 -d > "$key_file"
if [ ! -s "$key_file" ]; then
  echo "authorized_keys content is empty" >&2
  exit 1
fi

missing=""
if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then
  missing="$missing openssh-server"
fi
if ! command -v rsync >/dev/null 2>&1; then
  missing="$missing rsync"
fi
if ! command -v sudo >/dev/null 2>&1; then
  missing="$missing sudo"
fi

if [ -n "$missing" ]; then
  echo "container is missing required SSH tools:$missing" >&2
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    update_ok=0
    for i in 1 2 3 4 5; do
      if apt-get -o Acquire::Retries=3 update; then
        update_ok=1
        break
      fi
      sleep 3
    done
    if [ "$update_ok" -ne 1 ]; then
      echo "apt-get update failed while preparing SSH access; check container network or use an image with openssh-server/rsync/sudo preinstalled" >&2
      exit 1
    fi
    if ! apt-get install -y --no-install-recommends openssh-server rsync sudo; then
      echo "apt-get install failed while preparing SSH access; check container package repositories or use a preinstalled image" >&2
      exit 1
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y openssh-server rsync sudo || { echo "dnf install failed while preparing SSH access" >&2; exit 1; }
  elif command -v yum >/dev/null 2>&1; then
    yum install -y openssh-server rsync sudo || { echo "yum install failed while preparing SSH access" >&2; exit 1; }
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache openssh rsync sudo || { echo "apk add failed while preparing SSH access" >&2; exit 1; }
  else
    echo "no supported package manager found while preparing SSH access; use an image with openssh-server/rsync/sudo preinstalled" >&2
    exit 1
  fi
fi

if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then
  echo "openssh-server is not installed and could not be installed" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is not installed and could not be installed" >&2
  exit 1
fi
if command -v sudo >/dev/null 2>&1; then
  mkdir -p /etc/sudoers.d
  printf '%%s ALL=(ALL) NOPASSWD:ALL\n' "$user" > /etc/sudoers.d/99-cluster-platform-user
  chmod 0440 /etc/sudoers.d/99-cluster-platform-user
fi
write_error=0
mkdir -p "$home_dir/.ssh" || write_error=1
if [ "$write_error" -eq 0 ] && ! cmp -s "$key_file" "$home_dir/.ssh/authorized_keys" 2>/dev/null; then
  cp "$key_file" "$home_dir/.ssh/authorized_keys" || write_error=1
fi
if [ "$write_error" -eq 0 ]; then
  chmod 700 "$home_dir/.ssh" || write_error=1
  chmod 600 "$home_dir/.ssh/authorized_keys" || write_error=1
  chown -R "$user:$user" "$home_dir/.ssh" 2>/dev/null || chown -R "$user" "$home_dir/.ssh" 2>/dev/null || true
fi
if [ "$write_error" -ne 0 ] || [ ! -s "$home_dir/.ssh/authorized_keys" ]; then
  echo "failed to write $home_dir/.ssh/authorized_keys" >&2
  exit 1
fi

mkdir -p /run/sshd
mkdir -p /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/99-cluster-platform.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
StrictModes no
PermitRootLogin prohibit-password
EOF

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || true
  systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
elif command -v service >/dev/null 2>&1; then
  service ssh restart 2>/dev/null || service sshd restart 2>/dev/null || service ssh start 2>/dev/null || service sshd start 2>/dev/null || true
elif command -v rc-service >/dev/null 2>&1; then
  rc-service sshd restart 2>/dev/null || rc-service sshd start 2>/dev/null || true
else
  /usr/sbin/sshd 2>/dev/null || true
fi
`, shellSingleQuote(username), shellSingleQuote(encodedKey), shellSingleQuote(encodedSkeletonSeed))
	_, err := runCommandCombined("incus", "exec", container, "--", "sh", "-lc", script)
	return err
}

func syncContainerSSHKeys(payload IncusSSHKeysPayload) (err error) {
	name := strings.TrimSpace(payload.Name)
	if name == "" {
		return fmt.Errorf("container name is empty")
	}
	status := incusContainerStatus(name)
	if status == "" {
		return fmt.Errorf("container %s does not exist", name)
	}
	wasStopped := status == "stopped"
	if wasStopped {
		if len(payload.ManagedMounts) == 0 {
			removeContainerHomeMounts(name, payload.SSHUsername)
		} else if err := validateActiveManagedMounts(payload.ManagedMounts); err != nil {
			return err
		}
		if err := ensureContainerRunning(name, 60*time.Second); err != nil {
			return err
		}
		defer func() {
			_, stopErr := runCommandCombined("incus", "stop", name)
			if err == nil && stopErr != nil {
				err = stopErr
			}
		}()
	} else if status != "running" {
		if err := ensureContainerRunning(name, 60*time.Second); err != nil {
			return err
		}
	}
	return initializeContainerSSHWithMounts(name, payload.SSHUsername, payload.Mounts, payload.SSHKey)
}

func parseMount(value string) (source string, target string, readonly bool) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", "", false
	}
	if strings.HasSuffix(trimmed, ":ro") {
		readonly = true
		trimmed = strings.TrimSuffix(trimmed, ":ro")
	} else if strings.HasSuffix(trimmed, ":rw") {
		trimmed = strings.TrimSuffix(trimmed, ":rw")
	}
	parts := strings.SplitN(trimmed, ":", 2)
	source = parts[0]
	target = source
	if len(parts) == 2 && parts[1] != "" {
		target = parts[1]
	}
	return source, target, readonly
}

func addOrReplaceDevice(container string, deviceName string, args ...string) error {
	_, _ = runCommandCombined("incus", "config", "device", "remove", container, deviceName)
	_, err := runCommandCombined("incus", append([]string{"config", "device", "add", container, deviceName}, args...)...)
	return err
}

func proxyDeviceNames(container string) []string {
	output := runCommand("incus", "config", "device", "show", container)
	names := []string{}
	for _, line := range strings.Split(output, "\n") {
		if strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t") || !strings.HasSuffix(line, ":") {
			continue
		}
		name := strings.TrimSuffix(strings.TrimSpace(line), ":")
		if strings.HasPrefix(name, "proxy-") {
			names = append(names, name)
		}
	}
	return names
}

func diskDeviceNamesForPath(container string, path string) []string {
	targetPath := filepath.Clean(path)
	output := runCommand("incus", "config", "device", "show", container)
	names := []string{}
	currentName := ""
	currentType := ""
	currentPath := ""
	flush := func() {
		if currentName != "" && currentType == "disk" && currentPath != "" && filepath.Clean(currentPath) == targetPath {
			names = append(names, currentName)
		}
		currentName = ""
		currentType = ""
		currentPath = ""
	}
	for _, line := range strings.Split(output, "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		if !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") && strings.HasSuffix(strings.TrimSpace(line), ":") {
			flush()
			currentName = strings.TrimSuffix(strings.TrimSpace(line), ":")
			continue
		}
		if currentName == "" {
			continue
		}
		trimmed := strings.TrimSpace(line)
		key, value, ok := strings.Cut(trimmed, ":")
		if !ok {
			continue
		}
		value = strings.Trim(strings.TrimSpace(value), `"'`)
		switch key {
		case "type":
			currentType = value
		case "path":
			currentPath = value
		}
	}
	flush()
	return names
}

func removeContainerHomeMounts(container string, username string) {
	homePath := filepath.Clean("/home/" + strings.TrimSpace(username))
	if homePath == "/home" {
		homePath = "/home/ubuntu"
	}
	for _, name := range diskDeviceNamesForPath(container, homePath) {
		_, _ = runCommandCombined("incus", "config", "device", "remove", container, name)
	}
}

func removeProxyDevices(container string) {
	for _, name := range proxyDeviceNames(container) {
		_, _ = runCommandCombined("incus", "config", "device", "remove", container, name)
	}
}

func syncIncusPorts(container string, ports []IncusPort) error {
	removeProxyDevices(container)
	for index, port := range ports {
		protocol := strings.ToLower(strings.TrimSpace(port.Protocol))
		if protocol != "tcp" && protocol != "udp" {
			return fmt.Errorf("unsupported port protocol %s", port.Protocol)
		}
		listenPort := port.NodePort
		if listenPort == 0 {
			listenPort = port.HostPort
		}
		deviceID := port.ID
		if deviceID == 0 {
			deviceID = index
		}
		deviceName := safeDeviceName("proxy", deviceID, protocol, listenPort)
		err := addOrReplaceDevice(
			container,
			deviceName,
			"proxy",
			fmt.Sprintf("listen=%s:0.0.0.0:%d", protocol, listenPort),
			fmt.Sprintf("connect=%s:127.0.0.1:%d", protocol, port.ContainerPort),
		)
		if err != nil {
			return err
		}
	}
	return nil
}

func hasSSHPort(ports []IncusPort) bool {
	for _, port := range ports {
		if strings.ToLower(strings.TrimSpace(port.Protocol)) == "tcp" && port.ContainerPort == 22 {
			return true
		}
	}
	return false
}

func setRootDiskSize(container string, diskGB int, storagePool string) error {
	size := fmt.Sprintf("%dGiB", diskGB)
	if _, err := runCommandCombined("incus", "config", "device", "set", container, "root", "size", size); err == nil {
		return nil
	}
	if _, err := runCommandCombined("incus", "config", "device", "override", container, "root", "size="+size); err == nil {
		return nil
	}
	_, err := runCommandCombined("incus", "config", "device", "add", container, "root", "disk", "pool="+storagePool, "path=/", "size="+size)
	return err
}

func containerIP(container string) string {
	output := runCommand("incus", "list", container, "-c", "4", "--format", "csv")
	for _, field := range strings.FieldsFunc(output, func(r rune) bool { return r == ' ' || r == '\n' || r == '\t' || r == ',' }) {
		ip := net.ParseIP(field)
		if ip != nil && ip.To4() != nil && !ip.IsLoopback() {
			return ip.String()
		}
	}
	return ""
}

func waitContainerIP(container string, timeout time.Duration) string {
	deadline := time.Now().Add(timeout)
	for {
		ip := containerIP(container)
		if ip != "" || time.Now().After(deadline) {
			return ip
		}
		time.Sleep(time.Second)
	}
}

func incusContainerStatus(container string) string {
	return strings.ToLower(strings.TrimSpace(runCommand("incus", "list", container, "-c", "s", "--format", "csv")))
}

func incusContainerExists(container string) bool {
	return incusContainerStatus(container) != ""
}

func waitContainerRunning(container string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		status := incusContainerStatus(container)
		if status == "running" {
			return nil
		}
		if status == "" {
			return fmt.Errorf("container %s does not exist", container)
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("container %s did not reach running state, current status: %s", container, status)
		}
		time.Sleep(time.Second)
	}
}

func ensureContainerRunning(container string, timeout time.Duration) error {
	status := incusContainerStatus(container)
	if status == "" {
		return fmt.Errorf("container %s does not exist", container)
	}
	if status != "running" {
		if _, err := runCommandCombined("incus", "start", container); err != nil {
			return err
		}
	}
	return waitContainerRunning(container, timeout)
}

func isManagedMountSource(source string, dataPath string) bool {
	source = filepath.Clean(source)
	roots := []string{"/data", filepath.Clean(dataPath)}
	for _, root := range roots {
		if root == "." || root == "/" || root == "" {
			continue
		}
		if source == root || strings.HasPrefix(source, root+string(os.PathSeparator)) {
			return true
		}
	}
	return false
}

func ensureMountSource(source string, dataPath string) error {
	if source == "" {
		return nil
	}
	if _, err := os.Stat(source); err == nil {
		return nil
	}
	if !isManagedMountSource(source, dataPath) {
		return fmt.Errorf("mount source %s does not exist", source)
	}
	return os.MkdirAll(source, 0o755)
}

func normalizePCI(s string) string {
	// 00000000:03:00.0 -> 0000:03:00.0
	if strings.HasPrefix(s, "00000000:") {
		return "0000:" + strings.TrimPrefix(s, "00000000:")
	}
	return s
}

// ensureIncusStorageVolume creates an Incus storage volume if it doesn't already exist.
// sizeGB = 0 means no size limit (pool default).
func ensureIncusStorageVolume(pool, volumeName string, sizeGB int) error {
	// Check if volume already exists
	listOut := runCommand("incus", "storage", "volume", "list", pool, "-c", "n", "--format", "csv")
	for _, line := range strings.Split(listOut, "\n") {
		if strings.TrimSpace(line) == volumeName {
			return nil // already exists, reuse
		}
	}
	// Create the volume
	args := []string{"storage", "volume", "create", pool, volumeName}
	if sizeGB > 0 {
		args = append(args, fmt.Sprintf("size=%dGiB", sizeGB))
	}
	if _, err := runCommandCombined("incus", args...); err != nil {
		return err
	}
	return nil
}

func incusStorageVolumeExists(pool, volumeName string) bool {
	listOut := runCommand("incus", "storage", "volume", "list", pool, "-c", "n", "--format", "csv")
	for _, line := range strings.Split(listOut, "\n") {
		if strings.TrimSpace(line) == volumeName {
			return true
		}
	}
	return false
}

func executeRemoveUserWorkspaceVolume(payload UserWorkspaceVolumeRemovePayload, storagePool string) (string, error) {
	volumeName := strings.TrimSpace(payload.VolumeName)
	if volumeName == "" {
		return "", fmt.Errorf("volume_name is empty")
	}
	if !regexp.MustCompile(`^user-[0-9]+-ws$`).MatchString(volumeName) {
		return "", fmt.Errorf("refuse to remove unmanaged workspace volume %q", volumeName)
	}
	if storagePool == "" {
		storagePool = detectIncusStoragePool()
	}
	if storagePool == "" {
		return "", fmt.Errorf("incus storage pool 未配置，且无法自动探测")
	}
	if !incusStorageVolumeExists(storagePool, volumeName) {
		return fmt.Sprintf("workspace volume %s does not exist, skipped", volumeName), nil
	}
	output, err := runCommandCombined("incus", "storage", "volume", "delete", storagePool, "custom/"+volumeName)
	if err != nil {
		fallbackOutput, fallbackErr := runCommandCombined("incus", "storage", "volume", "delete", storagePool, volumeName)
		if fallbackErr != nil {
			return strings.TrimSpace(output + "\n" + fallbackOutput), fmt.Errorf("delete workspace volume %s: %w", volumeName, fallbackErr)
		}
		output = strings.TrimSpace(output + "\n" + fallbackOutput)
	}
	return strings.TrimSpace(fmt.Sprintf("removed workspace volume %s\n%s", volumeName, output)), nil
}

func executeIncusCreate(payload IncusCreatePayload, storagePool string, dataPath string) (string, error) {
	if payload.Name == "" {
		return "", fmt.Errorf("container name is empty")
	}
	if payload.Image == "" {
		payload.Image = "images:ubuntu/24.04"
	}
	if err := ensureSharedStorage(payload.SharedStorage, payload.ManagedMounts); err != nil {
		return "", err
	}
	if runCommand("incus", "info", payload.Name) == "" {
		// Use 'init' (create without starting) so all devices can be configured
		// before the container starts, avoiding hot-add delays that cause incus exec to hang.
		args := []string{
			"init",
			payload.Image,
			payload.Name,
			"--quiet",
			"-c", fmt.Sprintf("limits.cpu=%d", payload.CPUCores),
			"-c", fmt.Sprintf("limits.memory=%dGiB", payload.MemoryGB),
			"-c", "security.nesting=true",
			"-c", "security.syscalls.intercept.mknod=true",
			"-c", "security.syscalls.intercept.setxattr=true",
			"-c", "user.user-data=" + cloudInitUserData(payload.SSHUsername, payload.SSHKey),
		}
		if payload.SharedStorage.Enabled && payload.SharedStorage.IDMapBase >= 65536 {
			args = append(args, "-c", fmt.Sprintf("security.idmap.base=%d", payload.SharedStorage.IDMapBase))
		}
		if len(payload.GPUs) > 0 {
			args = append(args, "-c", "nvidia.runtime=true")
		}
		if storagePool != "" {
			args = append(args, "-s", storagePool)
		}
		if _, err := runCommandCombined("incus", args...); err != nil {
			return "", err
		}
		if len(payload.GPUs) > 0 {
			if _, err := runCommandCombined("incus", "config", "set", payload.Name, "nvidia.runtime", "true"); err != nil {
				return "", err
			}
		}
		if payload.DiskGB > 0 {
			if err := setRootDiskSize(payload.Name, payload.DiskGB, storagePool); err != nil {
				return "", err
			}
		}
		for index, gpu := range payload.GPUs {
			deviceName := safeDeviceName("gpu", index, gpu.Slot)
			gpuArgs := []string{"gpu"}
			if gpu.PCIAddress != "" {
				pci := normalizePCI(gpu.PCIAddress)
				// 先尝试精确 PCI
				err := addOrReplaceDevice(payload.Name, deviceName, append(gpuArgs, "pci="+pci)...)
				if err == nil {
					continue
				}
				// PCI 失败后，退回 id
				if gpu.Slot >= 0 {
					err = addOrReplaceDevice(payload.Name, deviceName, "gpu", fmt.Sprintf("id=%d", gpu.Slot))
					if err == nil {
						continue
					}
				}
				// 最后退回裸 gpu
				err = addOrReplaceDevice(payload.Name, deviceName, "gpu")
				if err != nil {
					return "", err
				}
				continue
			}
			if gpu.Slot >= 0 {
				if err := addOrReplaceDevice(payload.Name, deviceName, "gpu", fmt.Sprintf("id=%d", gpu.Slot)); err == nil {
					continue
				}
			}
			if err := addOrReplaceDevice(payload.Name, deviceName, "gpu"); err != nil {
				return "", err
			}
		}
		if len(payload.ManagedMounts) > 0 {
			for index, mount := range payload.ManagedMounts {
				if err := validateManagedMount(mount); err != nil {
					return "", err
				}
				args := []string{"disk", "source=" + mount.Source, "path=" + mount.Target, fmt.Sprintf("required=%t", mount.Required)}
				if mount.Readonly {
					args = append(args, "readonly=true")
				}
				if err := addOrReplaceDevice(payload.Name, safeDeviceName("managed", index, mount.Target), args...); err != nil {
					return "", fmt.Errorf("attach managed mount %s: %w", mount.Target, err)
				}
			}
		} else {
			for index, mount := range payload.Mounts {
				source, target, readonly := parseMount(mount)
				if source == "" || target == "" {
					continue
				}
				if isContainerHomePath(target, payload.SSHUsername) {
					fmt.Fprintf(os.Stderr, "%s skip container home mount %s -> %s\n", time.Now().Format(time.RFC3339), source, target)
					continue
				}
				if err := ensureMountSource(source, dataPath); err != nil {
					return "", err
				}
				args := []string{"disk", "source=" + source, "path=" + target}
				if readonly {
					args = append(args, "readonly=true")
				}
				if err := addOrReplaceDevice(payload.Name, safeDeviceName("disk", index, target), args...); err != nil {
					return "", err
				}
			}
		}
		// workspace: named Incus storage volume mounted at /workspace (reusable across containers)
		if payload.WorkspaceVolumeName != "" && storagePool != "" {
			volSize := payload.WorkspaceVolumeGB
			if err := ensureIncusStorageVolume(storagePool, payload.WorkspaceVolumeName, volSize); err != nil {
				return "", fmt.Errorf("create workspace volume: %w", err)
			}
			wsArgs := []string{"disk", "pool=" + storagePool, "source=" + payload.WorkspaceVolumeName, "path=/workspace"}
			if err := addOrReplaceDevice(payload.Name, "workspace", wsArgs...); err != nil {
				return "", fmt.Errorf("attach workspace volume: %w", err)
			}
		}
	}
	if len(payload.ManagedMounts) == 0 {
		removeContainerHomeMounts(payload.Name, payload.SSHUsername)
	}
	// Start or wait for the container before incus exec; retries may find an
	// existing stopped instance from a previous failed create attempt.
	if err := ensureContainerRunning(payload.Name, 60*time.Second); err != nil {
		return "", err
	}
	if err := syncIncusPorts(payload.Name, payload.Ports); err != nil {
		return "", err
	}
	return containerIP(payload.Name), nil
}

// executeIncusConfigUpdate applies CPU/memory/GPU changes to a running or stopped container.
func executeIncusConfigUpdate(payload IncusConfigUpdatePayload, storagePool string) error {
	name := strings.TrimSpace(payload.Name)
	if name == "" {
		return fmt.Errorf("container name is empty")
	}
	status := incusContainerStatus(name)
	if status == "" {
		return fmt.Errorf("container %s does not exist", name)
	}
	// Update CPU and memory limits (safe to apply while running)
	if payload.CPUCores > 0 {
		if _, err := runCommandCombined("incus", "config", "set", name, fmt.Sprintf("limits.cpu=%d", payload.CPUCores)); err != nil {
			return fmt.Errorf("set CPU: %w", err)
		}
	}
	if payload.MemoryGB > 0 {
		if _, err := runCommandCombined("incus", "config", "set", name, fmt.Sprintf("limits.memory=%dGiB", payload.MemoryGB)); err != nil {
			return fmt.Errorf("set memory: %w", err)
		}
	}
	// Update GPU devices: remove all existing gpu-* devices then re-add
	output := runCommand("incus", "config", "device", "show", name)
	currentGPUDevices := []string{}
	for _, line := range strings.Split(output, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "gpu-") && strings.HasSuffix(trimmed, ":") {
			currentGPUDevices = append(currentGPUDevices, strings.TrimSuffix(trimmed, ":"))
		}
	}
	for _, devName := range currentGPUDevices {
		_, _ = runCommandCombined("incus", "config", "device", "remove", name, devName)
	}
	for index, gpu := range payload.GPUs {
		deviceName := safeDeviceName("gpu", index, gpu.Slot)
		gpuArgs := []string{"gpu"}
		if gpu.PCIAddress != "" {
			pci := normalizePCI(gpu.PCIAddress)
			if err := addOrReplaceDevice(name, deviceName, append(gpuArgs, "pci="+pci)...); err == nil {
				continue
			}
		}
		if gpu.Slot >= 0 {
			if err := addOrReplaceDevice(name, deviceName, "gpu", fmt.Sprintf("id=%d", gpu.Slot)); err == nil {
				continue
			}
		}
		if err := addOrReplaceDevice(name, deviceName, "gpu"); err != nil {
			return fmt.Errorf("add GPU device: %w", err)
		}
	}
	// Update nvidia.runtime flag
	if len(payload.GPUs) > 0 {
		_, _ = runCommandCombined("incus", "config", "set", name, "nvidia.runtime", "true")
	} else {
		_, _ = runCommandCombined("incus", "config", "unset", name, "nvidia.runtime")
	}
	return nil
}

func executeIncusLifecycle(payload IncusLifecyclePayload) (string, error) {
	name := strings.TrimSpace(payload.Name)
	if name == "" {
		return "", fmt.Errorf("container name is empty")
	}
	operation := strings.ToLower(strings.TrimSpace(payload.Operation))
	if operation == "start" || operation == "restart" {
		if err := ensureSharedStorage(payload.SharedStorage, payload.ManagedMounts); err != nil {
			return "", err
		}
	}
	status := incusContainerStatus(name)
	switch operation {
	case "start":
		if status == "running" {
			return waitContainerIP(name, 15*time.Second), nil
		}
		if status == "" {
			return "", fmt.Errorf("container %s does not exist", name)
		}
		if _, err := runCommandCombined("incus", "start", name); err != nil {
			return "", err
		}
		return waitContainerIP(name, 20*time.Second), nil
	case "stop":
		if status == "stopped" {
			return "", nil
		}
		if status == "" {
			return "", fmt.Errorf("container %s does not exist", name)
		}
		if _, err := runCommandCombined("incus", "stop", name); err != nil {
			return "", err
		}
		return "", nil
	case "restart":
		if status == "" {
			return "", fmt.Errorf("container %s does not exist", name)
		}
		if status == "stopped" {
			if _, err := runCommandCombined("incus", "start", name); err != nil {
				return "", err
			}
		} else if _, err := runCommandCombined("incus", "restart", name); err != nil {
			return "", err
		}
		return waitContainerIP(name, 20*time.Second), nil
	case "delete":
		if !incusContainerExists(name) {
			return "", nil
		}
		_, err := runCommandCombined("incus", "delete", name, "--force")
		return "", err
	default:
		return "", fmt.Errorf("unsupported lifecycle operation: %s", payload.Operation)
	}
}

func scheduleNodePower(operation string) error {
	command := "systemctl poweroff"
	if operation == "reboot" {
		command = "systemctl reboot"
	}
	return exec.Command("sh", "-c", fmt.Sprintf("(sleep 2; %s) >/dev/null 2>&1 &", command)).Start()
}

func exportFilePriority(path string) int {
	name := strings.ToLower(filepath.Base(path))
	if strings.Contains(name, ".rootfs.") {
		return 2
	}
	if strings.HasSuffix(name, ".tar.xz") || strings.HasSuffix(name, ".tar.gz") || strings.HasSuffix(name, ".tar") {
		return 1
	}
	return 3
}

func matchingExportFiles(dir string, baseName string) ([]string, int64, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, 0, err
	}
	files := []string{}
	var size int64
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), baseName) {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		info, err := entry.Info()
		if err == nil {
			size += info.Size()
		}
		files = append(files, path)
	}
	sort.Slice(files, func(i, j int) bool {
		left := exportFilePriority(files[i])
		right := exportFilePriority(files[j])
		if left != right {
			return left < right
		}
		return files[i] < files[j]
	})
	return files, size, nil
}

func cleanPreviousExportFiles(dir string, baseName string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), baseName) {
			continue
		}
		if err := os.Remove(filepath.Join(dir, entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

func executeIncusImageExport(payload IncusImageExportPayload) (string, error) {
	imageRef := strings.TrimSpace(payload.ImageRef)
	if imageRef == "" {
		return "", fmt.Errorf("image_ref is empty")
	}
	exportDir, err := cleanSyncPath(payload.ExportDir)
	if err != nil {
		return "", err
	}
	baseName := strings.TrimSpace(payload.BaseName)
	if baseName == "" || strings.ContainsAny(baseName, `/\`) {
		return "", fmt.Errorf("base_name is invalid")
	}
	if err := os.MkdirAll(exportDir, 0o755); err != nil {
		return "", err
	}
	if err := cleanPreviousExportFiles(exportDir, baseName); err != nil {
		return "", err
	}
	outputBase := filepath.Join(exportDir, baseName)
	output, err := runCommandCombinedTimeout(60*time.Minute, "incus", "image", "export", imageRef, outputBase)
	if err != nil {
		return output, err
	}
	files, size, err := matchingExportFiles(exportDir, baseName)
	if err != nil {
		return output, err
	}
	if len(files) == 0 {
		return output, fmt.Errorf("incus image export produced no files for %s", baseName)
	}
	result, _ := json.Marshal(map[string]any{
		"export_dir": exportDir,
		"base_name":  baseName,
		"files":      files,
		"size_bytes": size,
	})
	return string(result), nil
}

func executeIncusImageCleanup(payload IncusImageCleanupPayload) (string, error) {
	exportDir := strings.TrimSpace(payload.ExportDir)
	baseName := strings.TrimSpace(payload.BaseName)
	if exportDir == "" || baseName == "" {
		return "", fmt.Errorf("export_dir or base_name is empty")
	}
	if strings.ContainsAny(baseName, `/\`) || strings.ContainsAny(exportDir, "\r\n") {
		return "", fmt.Errorf("invalid export_dir or base_name")
	}
	// 清理导出的镜像文件
	files, _, err := matchingExportFiles(exportDir, baseName)
	if err != nil {
		return "", fmt.Errorf("finding export files: %w", err)
	}
	var removed []string
	for _, f := range files {
		fullPath := filepath.Join(exportDir, f)
		if err := os.Remove(fullPath); err != nil {
			if !os.IsNotExist(err) {
				return "", fmt.Errorf("removing %s: %w", fullPath, err)
			}
		} else {
			removed = append(removed, f)
		}
	}
	// 尝试从 incus 中删除镜像（如果已导入）
	if fp := strings.TrimSpace(payload.Fingerprint); fp != "" {
		runCommand("incus", "image", "delete", fp)
	}
	return fmt.Sprintf("cleaned up %d files: %s", len(removed), strings.Join(removed, ", ")), nil
}

func executeIncusImageImport(payload IncusImageImportPayload, dataPath string) (string, error) {
	if strings.TrimSpace(payload.Alias) != "" && runCommand("incus", "image", "info", strings.TrimSpace(payload.Alias)) != "" {
		return "image already available: " + strings.TrimSpace(payload.Alias), nil
	}
	if strings.TrimSpace(payload.Fingerprint) != "" && runCommand("incus", "image", "info", strings.TrimSpace(payload.Fingerprint)) != "" {
		return "image already available: " + strings.TrimSpace(payload.Fingerprint), nil
	}
	syncOutput, err := executeDataSync(DataSyncPayload{
		SourceNodeID:   payload.SourceNodeID,
		TargetNodeID:   payload.TargetNodeID,
		SourcePath:     payload.SourcePath,
		TargetPath:     payload.TargetPath,
		Mode:           "incus_image_import",
		Delete:         true,
		SourceEndpoint: payload.SourceEndpoint,
	}, dataPath, nil)
	if err != nil {
		return syncOutput, err
	}
	targetPath, err := cleanSyncPath(payload.TargetPath)
	if err != nil {
		return syncOutput, err
	}
	files, _, err := matchingExportFiles(targetPath, payload.BaseName)
	if err != nil {
		return syncOutput, err
	}
	if len(files) == 0 {
		return syncOutput, fmt.Errorf("no exported image files found in %s", targetPath)
	}
	args := append([]string{"image", "import"}, files...)
	if strings.TrimSpace(payload.Alias) != "" {
		args = append(args, "--alias", strings.TrimSpace(payload.Alias))
	}
	output, err := runCommandCombinedTimeout(60*time.Minute, "incus", args...)
	if err != nil {
		if output == "" {
			output = syncOutput
		}
		return output, err
	}
	if output == "" {
		output = "imported " + strings.TrimSpace(payload.Alias)
	}
	if syncOutput != "" {
		output = syncOutput + "\n" + output
	}
	return output, nil
}
