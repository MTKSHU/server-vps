package main

import (
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func cleanSyncPath(path string) (string, error) {
	cleaned := filepath.Clean(strings.TrimSpace(path))
	if cleaned == "." || cleaned == "" || !filepath.IsAbs(cleaned) {
		return "", fmt.Errorf("sync path must be absolute: %s", path)
	}
	return cleaned, nil
}

func executeSharedResourceVerify(payload SharedResourceVerifyPayload) (string, error) {
	source, err := cleanSyncPath(payload.SourcePath)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(source)
	if err != nil {
		return "", fmt.Errorf("resource path not available: %w", err)
	}
	var fileCount int64
	var sizeBytes int64
	if !info.IsDir() {
		fileCount = 1
		sizeBytes = info.Size()
	} else {
		err = filepath.WalkDir(source, func(path string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if entry.IsDir() {
				return nil
			}
			info, err := entry.Info()
			if err != nil {
				return err
			}
			fileCount++
			sizeBytes += info.Size()
			return nil
		})
		if err != nil {
			return "", err
		}
	}
	result, _ := json.Marshal(map[string]any{
		"resource_id":   payload.ResourceID,
		"resource_type": payload.ResourceType,
		"name":          payload.Name,
		"version":       payload.Version,
		"source_path":   source,
		"file_count":    fileCount,
		"size_bytes":    sizeBytes,
	})
	return string(result), nil
}
func syncSSHParts(endpoint DataSyncSSHEndpoint) (string, string, []string, error) {
	host := strings.TrimSpace(endpoint.Host)
	user := strings.TrimSpace(endpoint.User)
	if host == "" {
		return "", "", nil, fmt.Errorf("sync endpoint host is empty")
	}
	if user == "" {
		user = "root"
	}
	if strings.ContainsAny(host, " \t\r\n@:") || strings.ContainsAny(user, " \t\r\n@:") {
		return "", "", nil, fmt.Errorf("sync endpoint user or host contains invalid characters")
	}
	port := endpoint.Port
	if port <= 0 {
		port = 22
	}
	knownHostsFile := "/tmp/cluster-node-agent-known-hosts"
	identityFile := strings.TrimSpace(endpoint.IdentityFile)
	jump := strings.TrimSpace(endpoint.JumpHost)

	// 构建 ssh 基础参数
	sshParts := []string{
		"ssh",
		"-p", strconv.Itoa(port),
		"-o", "BatchMode=yes",
		"-o", "StrictHostKeyChecking=no",
		"-o", "UserKnownHostsFile=" + knownHostsFile,
	}

	if jump != "" {
		// 验证跳板机地址格式，避免注入：只允许 user@host 或 user@host:port
		if strings.ContainsAny(jump, " \t\r\n") || strings.Count(jump, "@") != 1 {
			return "", "", nil, fmt.Errorf("invalid jump_host format: %s", jump)
		}
		// 解析跳板机地址
		jumpHost := jump[strings.Index(jump, "@")+1:]
		jumpPort := "22"
		if idx := strings.LastIndex(jumpHost, ":"); idx != -1 {
			jumpPort = jumpHost[idx+1:]
			jumpHost = jumpHost[:idx]
		}
		// 预收集跳板机和目标主机的 host key
		ensureHostKeys(knownHostsFile, jumpHost, jumpPort, host, strconv.Itoa(port))

		// 使用 ProxyCommand 代替 -J，确保跳板机连接也使用相同的
		// StrictHostKeyChecking=no + UserKnownHostsFile 设置。
		// SSH 的 -J 选项在连接跳板机时不遵循 -o 选项，而 ProxyCommand
		// 启动的子 ssh 进程可以显式传递所有选项。
		// 注意：ProxyCommand 值包含空格，需要确保在 rsync -e 传给 shell
		// 时被正确引用。这里将整个 proxy 命令作为 -o 的单独值传递，
		// 在 exec.Command 层面（不经过 shell）是正确的；
		// 在 rsync -e 场景中，strings.Join 后由 sh -c 解析，
		// 需要用单引号包裹 ProxyCommand 的值部分。
		proxyCmd := fmt.Sprintf(
			"ssh -p %s -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=%s -W %%h:%%p %s",
			jumpPort, knownHostsFile, jump,
		)
		if identityFile != "" {
			proxyCmd = fmt.Sprintf(
				"ssh -p %s -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=%s -i %s -W %%h:%%p %s",
				jumpPort, knownHostsFile, identityFile, jump,
			)
		}
		// exec.Command 直接传递时不需要引号
		// 但 rsync -e 传给 sh -c 时需要引号，所以存两份格式
		sshParts = append(sshParts, "-o", "ProxyCommand="+proxyCmd)
	} else if identityFile != "" {
		sshParts = append(sshParts, "-i", identityFile)
	}

	return user, host, sshParts, nil
}

// ensureHostKeys 使用 ssh-keyscan 预收集主机密钥到 known_hosts 文件
func ensureHostKeys(knownHostsFile string, hostsAndPorts ...string) {
	// 确保 known_hosts 文件所在目录存在
	if err := os.MkdirAll(filepath.Dir(knownHostsFile), 0755); err != nil {
		return
	}
	// 构建 ssh-keyscan 目标列表：host:port host2:port2 ...
	var targets []string
	for i := 0; i < len(hostsAndPorts); i += 2 {
		if i+1 < len(hostsAndPorts) {
			targets = append(targets, fmt.Sprintf("[%s]:%s", hostsAndPorts[i], hostsAndPorts[i+1]))
		}
	}
	if len(targets) == 0 {
		return
	}
	// 用 ssh-keyscan 收集主机密钥并追加到 known_hosts
	// 超时设为 5 秒，避免在网络不可达时长时间阻塞
	args := append([]string{"-T", "5"}, targets...)
	cmd := exec.Command("ssh-keyscan", args...)
	output, err := cmd.Output()
	if err != nil {
		// ssh-keyscan 失败时静默跳过，StrictHostKeyChecking=no 会在连接时自动接受
		return
	}
	if len(output) > 0 {
		f, err := os.OpenFile(knownHostsFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			return
		}
		defer f.Close()
		f.Write(output)
	}
}

// makeRestrictedRsyncPath 把绝对路径转换为相对于 allowedPath 的相对路径。
// 当远端使用 rrsync 并通过 command= 限制允许目录时，rsync 客户端必须使用相对路径，
// 否则 rrsync 会把客户端传入的绝对路径再次拼接到允许目录上，导致路径重复。
func makeRestrictedRsyncPath(allowedPath, path string) (string, error) {
	allowed := filepath.Clean(strings.TrimSpace(allowedPath))
	cleaned := filepath.Clean(strings.TrimSpace(path))
	if !filepath.IsAbs(allowed) {
		return "", fmt.Errorf("allowed path must be absolute: %s", allowedPath)
	}
	if cleaned == allowed {
		return ".", nil
	}
	prefix := allowed + string(os.PathSeparator)
	if !strings.HasPrefix(cleaned, prefix) {
		return "", fmt.Errorf("path %s is outside allowed path %s", path, allowedPath)
	}
	return strings.TrimPrefix(cleaned, prefix), nil
}

func remoteRsyncSource(endpoint DataSyncSSHEndpoint, path string) (string, []string, error) {
	user, host, sshParts, err := syncSSHParts(endpoint)
	if err != nil {
		return "", nil, err
	}
	remotePath := strings.TrimSuffix(path, string(os.PathSeparator)) + string(os.PathSeparator)
	if endpoint.Restricted && strings.TrimSpace(endpoint.AllowedPath) != "" {
		rel, err := makeRestrictedRsyncPath(endpoint.AllowedPath, path)
		if err != nil {
			return "", nil, err
		}
		remotePath = rel
		if remotePath == "." {
			remotePath = "./"
		} else if !strings.HasSuffix(remotePath, string(os.PathSeparator)) {
			remotePath += string(os.PathSeparator)
		}
	}
	if strings.ContainsAny(remotePath, "\r\n") {
		return "", nil, fmt.Errorf("source path contains invalid characters")
	}
	// 构建 -e 参数：rsync 会把这个字符串传给 /bin/sh -c
	// ProxyCommand 的值包含空格，需要用单引号包裹
	sshCmd := buildShellSSHCommand(sshParts)
	return fmt.Sprintf("%s@%s:%s", user, host, remotePath), []string{"-e", sshCmd}, nil
}

func remoteRsyncTarget(endpoint DataSyncSSHEndpoint, path string) (string, []string, error) {
	user, host, sshParts, err := syncSSHParts(endpoint)
	if err != nil {
		return "", nil, err
	}
	cleaned, err := cleanSyncPath(path)
	if err != nil {
		return "", nil, err
	}
	if !endpoint.Restricted {
		mkdirArgs := append(append([]string{}, sshParts[1:]...), user+"@"+host, "mkdir -p -- "+shellSingleQuote(cleaned))
		if _, err := runCommandCombined("ssh", mkdirArgs...); err != nil {
			return "", nil, err
		}
	}
	sshCmd := buildShellSSHCommand(sshParts)
	remotePath := strings.TrimSuffix(cleaned, "/") + "/"
	if endpoint.Restricted && strings.TrimSpace(endpoint.AllowedPath) != "" {
		rel, err := makeRestrictedRsyncPath(endpoint.AllowedPath, path)
		if err != nil {
			return "", nil, err
		}
		remotePath = rel
		if remotePath == "." {
			remotePath = "./"
		} else if !strings.HasSuffix(remotePath, string(os.PathSeparator)) {
			remotePath += string(os.PathSeparator)
		}
	}
	return fmt.Sprintf("%s@%s:%s", user, host, remotePath), []string{"-e", sshCmd}, nil
}

// buildShellSSHCommand 将 sshParts 拼接为适合 rsync -e 的 shell 命令字符串。
// ProxyCommand 的值包含空格，需要用单引号包裹以确保 shell 正确解析。
func buildShellSSHCommand(sshParts []string) string {
	var parts []string
	for i := 0; i < len(sshParts); i++ {
		part := sshParts[i]
		// 检测 -o ProxyCommand=... 选项，用单引号包裹值部分
		if strings.HasPrefix(part, "-o") && i+1 < len(sshParts) && strings.HasPrefix(sshParts[i+1], "ProxyCommand=") {
			proxyVal := sshParts[i+1][len("ProxyCommand="):]
			parts = append(parts, part, "ProxyCommand='"+proxyVal+"'")
			i++ // 跳过下一个元素
		} else {
			parts = append(parts, part)
		}
	}
	return strings.Join(parts, " ")
}

func userScopedPath(path string, username string) bool {
	parts := strings.Split(filepath.ToSlash(filepath.Clean(path)), "/")
	for index := 0; index+1 < len(parts); index++ {
		if parts[index] == "users" && parts[index+1] == username {
			return true
		}
	}
	return false
}

func restoreManagedPath(path string, dataPath string) bool {
	if filepath.Clean(dataPath) != string(os.PathSeparator) {
		return pathWithinRoot(path, dataPath)
	}
	return pathWithinRoot(path, "/data") || pathWithinRoot(path, "/scratch")
}

func resolveWithExistingAncestor(path string) (string, error) {
	cleaned := filepath.Clean(path)
	current := cleaned
	missing := []string{}
	for {
		if _, err := os.Lstat(current); err == nil {
			resolved, err := filepath.EvalSymlinks(current)
			if err != nil {
				return "", err
			}
			for index := len(missing) - 1; index >= 0; index-- {
				resolved = filepath.Join(resolved, missing[index])
			}
			return filepath.Clean(resolved), nil
		} else if !os.IsNotExist(err) {
			return "", err
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", fmt.Errorf("cannot resolve existing ancestor for %s", path)
		}
		missing = append(missing, filepath.Base(current))
		current = parent
	}
}

func validateRestorePayload(payload DataSyncPayload, dataPath string) error {
	if strings.TrimSpace(payload.Username) == "" {
		return fmt.Errorf("restore username is empty")
	}
	sourceRoot, err := cleanSyncPath(payload.SourceRoot)
	if err != nil {
		return err
	}
	targetRoot, err := cleanSyncPath(payload.TargetRoot)
	if err != nil {
		return err
	}
	if !pathWithinRoot(payload.SourcePath, sourceRoot) || !pathWithinRoot(payload.TargetPath, targetRoot) {
		return fmt.Errorf("restore path escapes declared root")
	}
	if !userScopedPath(sourceRoot, payload.Username) || !userScopedPath(targetRoot, payload.Username) {
		return fmt.Errorf("restore root does not match user scope")
	}
	if !restoreManagedPath(sourceRoot, dataPath) || !restoreManagedPath(targetRoot, dataPath) {
		return fmt.Errorf("restore root is outside managed data path")
	}
	resolvedSourceRoot, err := filepath.EvalSymlinks(sourceRoot)
	if err != nil {
		return err
	}
	resolvedSource, err := filepath.EvalSymlinks(payload.SourcePath)
	if err != nil {
		return err
	}
	if !pathWithinRoot(resolvedSource, resolvedSourceRoot) || !restoreManagedPath(resolvedSourceRoot, dataPath) {
		return fmt.Errorf("restore source symlink escapes backup root")
	}
	resolvedTargetRoot, err := resolveWithExistingAncestor(targetRoot)
	if err != nil {
		return err
	}
	resolvedTarget, err := resolveWithExistingAncestor(payload.TargetPath)
	if err != nil {
		return err
	}
	if !pathWithinRoot(resolvedTarget, resolvedTargetRoot) || !restoreManagedPath(resolvedTargetRoot, dataPath) {
		return fmt.Errorf("restore target symlink escapes target root")
	}
	return nil
}

func incusFileTarget(container string, path string) (string, error) {
	name := strings.TrimSpace(container)
	if name == "" {
		return "", fmt.Errorf("container name is empty")
	}
	cleaned, err := cleanSyncPath(path)
	if err != nil {
		return "", err
	}
	if strings.ContainsAny(name, " \t\r\n:/") {
		return "", fmt.Errorf("container name contains invalid characters")
	}
	return name + cleaned, nil
}

func executeContainerDataSync(payload DataSyncPayload, dataPath string) (string, error) {
	mode := strings.TrimSpace(payload.Mode)
	if mode != "storage_to_container" && mode != "container_to_storage" {
		return "", fmt.Errorf("unsupported container sync mode: %s", payload.Mode)
	}
	if strings.TrimSpace(payload.ContainerName) == "" {
		return "", fmt.Errorf("container name is empty")
	}
	if !incusContainerExists(payload.ContainerName) {
		return "", fmt.Errorf("container %s does not exist", payload.ContainerName)
	}
	// 优先使用 /mnt/data/tmp 作为临时目录（避免 /tmp 是 tmpfs 且空间不足）
	tmpParent := "/mnt/data/tmp"
	if _, err := os.Stat(tmpParent); os.IsNotExist(err) {
		tmpParent = "" // 回退到系统默认临时目录
	}
	tmpDir, err := os.MkdirTemp(tmpParent, "cluster-container-sync-*")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tmpDir)

	switch mode {
	case "storage_to_container":
		staging := filepath.Join(tmpDir, "payload")
		if err := os.MkdirAll(staging, 0755); err != nil {
			return "", err
		}
		stagePayload := payload
		stagePayload.Mode = ""
		stagePayload.TargetPath = staging
		stagePayload.TargetEndpoint = DataSyncSSHEndpoint{}
		output, err := executeDataSync(stagePayload, dataPath)
		if err != nil {
			return output, err
		}
		target, err := incusFileTarget(payload.ContainerName, payload.TargetPath)
		if err != nil {
			return output, err
		}
		if _, err := runCommandCombined("incus", "exec", payload.ContainerName, "--", "mkdir", "-p", payload.TargetPath); err != nil {
			return output, err
		}
		entries, err := os.ReadDir(staging)
		if err != nil {
			return output, err
		}
		var pushOutput string
		for _, entry := range entries {
			srcPath := filepath.Join(staging, entry.Name())
			dstTarget := target + "/" + entry.Name()
			var out string
			if entry.IsDir() {
				out, err = runCommandCombined("incus", "file", "push", "-r", "-p", srcPath, dstTarget)
			} else {
				out, err = runCommandCombined("incus", "file", "push", "-p", srcPath, dstTarget)
			}
			pushOutput += out
			if err != nil {
				return output + pushOutput, err
			}
		}
		return strings.TrimSpace(output + "\n" + pushOutput), nil
	case "container_to_storage":
		staging := filepath.Join(tmpDir, "payload")
		source, err := incusFileTarget(payload.ContainerName, payload.SourcePath)
		if err != nil {
			return "", err
		}
		pullOutput, err := runCommandCombined("incus", "file", "pull", "-r", source, staging)
		if err != nil {
			return pullOutput, err
		}
		stagePayload := payload
		stagePayload.Mode = ""
		stagePayload.SourcePath = staging
		stagePayload.SourceEndpoint = DataSyncSSHEndpoint{}
		output, err := executeDataSync(stagePayload, dataPath)
		if err != nil {
			return pullOutput + output, err
		}
		return strings.TrimSpace(pullOutput + "\n" + output), nil
	}
	return "", fmt.Errorf("unsupported container sync mode: %s", payload.Mode)
}

func prepareSyncEndpoint(endpoint DataSyncSSHEndpoint) (DataSyncSSHEndpoint, string, error) {
	key := strings.TrimSpace(endpoint.PrivateKey)
	if key == "" {
		return endpoint, "", nil
	}
	f, err := os.CreateTemp("", "cluster-sync-key-*")
	if err != nil {
		return endpoint, "", fmt.Errorf("create temp key file: %w", err)
	}
	if _, err := f.WriteString(key); err != nil {
		f.Close()
		return endpoint, "", fmt.Errorf("write temp key file: %w", err)
	}
	if err := f.Chmod(0600); err != nil {
		f.Close()
		return endpoint, "", fmt.Errorf("chmod temp key file: %w", err)
	}
	if err := f.Close(); err != nil {
		return endpoint, "", fmt.Errorf("close temp key file: %w", err)
	}
	endpoint.IdentityFile = f.Name()
	endpoint.PrivateKey = ""
	// Restricted 标记保留，供后续跳过 mkdir 等操作
	return endpoint, f.Name(), nil
}

func executeDataSync(payload DataSyncPayload, dataPath string) (string, error) {
	source, err := cleanSyncPath(payload.SourcePath)
	if err != nil {
		return "", err
	}
	if payload.Mode == "restore_user_home" {
		if err := validateRestorePayload(payload, dataPath); err != nil {
			return "", err
		}
	}
	target, err := cleanSyncPath(payload.TargetPath)
	if err != nil {
		return "", err
	}
	remoteMode := payload.SourceEndpoint.Host != "" && payload.SourceNodeID != 0 && payload.SourceNodeID != payload.TargetNodeID
	remoteTargetMode := payload.TargetEndpoint.Host != "" && payload.SourceNodeID != 0 && payload.SourceNodeID != payload.TargetNodeID
	if remoteMode && remoteTargetMode {
		return "", fmt.Errorf("source and target cannot both be remote")
	}
	if source == target && !remoteMode && !remoteTargetMode {
		if err := os.MkdirAll(target, 0755); err != nil {
			return "", err
		}
		return fmt.Sprintf("prepared %s", target), nil
	}

	var keyFiles []string
	defer func() {
		for _, kf := range keyFiles {
			_ = os.Remove(kf)
		}
	}()
	if remoteMode {
		var kf string
		payload.SourceEndpoint, kf, err = prepareSyncEndpoint(payload.SourceEndpoint)
		if err != nil {
			return "", err
		}
		if kf != "" {
			keyFiles = append(keyFiles, kf)
		}
	}
	if remoteTargetMode {
		var kf string
		payload.TargetEndpoint, kf, err = prepareSyncEndpoint(payload.TargetEndpoint)
		if err != nil {
			return "", err
		}
		if kf != "" {
			keyFiles = append(keyFiles, kf)
		}
	}

	var sourceInfo os.FileInfo
	if !remoteMode {
		info, err := os.Stat(source)
		if err != nil {
			return "", fmt.Errorf("source path not available: %w", err)
		}
		sourceInfo = info
	}
	targetDirectory := target
	if sourceInfo != nil && !sourceInfo.IsDir() {
		targetDirectory = filepath.Dir(target)
	}
	if !remoteTargetMode {
		if err := os.MkdirAll(targetDirectory, 0755); err != nil {
			return "", err
		}
	}
	args := []string{"-a"}
	if payload.Update {
		args = append(args, "--update")
	}
	if payload.Delete && (sourceInfo == nil || sourceInfo.IsDir()) {
		args = append(args, "--delete")
	}
	if payload.IgnoreExisting {
		args = append(args, "--ignore-existing")
	}
	if payload.BandwidthLimit > 0 {
		args = append(args, "--bwlimit="+strconv.Itoa(payload.BandwidthLimit*1024))
	}
	rsyncSource := source
	rsyncTarget := target
	if sourceInfo == nil || sourceInfo.IsDir() {
		rsyncSource += string(os.PathSeparator)
		rsyncTarget += string(os.PathSeparator)
	}
	if remoteMode {
		remoteSource, remoteArgs, err := remoteRsyncSource(payload.SourceEndpoint, source)
		if err != nil {
			return "", err
		}
		args = append(args, remoteArgs...)
		rsyncSource = remoteSource
	}
	if remoteTargetMode {
		remoteTarget, remoteArgs, err := remoteRsyncTarget(payload.TargetEndpoint, target)
		if err != nil {
			return "", err
		}
		args = append(args, remoteArgs...)
		rsyncTarget = remoteTarget
	}
	args = append(args, rsyncSource, rsyncTarget)
	output, err := runCommandCombined("rsync", args...)
	if err != nil {
		return output, err
	}
	if output == "" {
		output = fmt.Sprintf("synced %s -> %s", source, target)
	}
	return output, nil
}

func findRrsync() (string, error) {
	if p, err := exec.LookPath("rrsync"); err == nil {
		return p, nil
	}
	candidates := []string{
		"/usr/local/bin/rrsync",
		"/usr/share/doc/rsync/scripts/rrsync",
	}
	for _, p := range candidates {
		if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
			return p, nil
		}
	}
	gzPath := "/usr/share/doc/rsync/scripts/rrsync.gz"
	if _, err := os.Stat(gzPath); err == nil {
		return installRrsyncFromGz(gzPath)
	}
	return "", fmt.Errorf("rrsync not found; install rsync package first")
}

func installRrsyncFromGz(gzPath string) (string, error) {
	f, err := os.Open(gzPath)
	if err != nil {
		return "", err
	}
	defer f.Close()
	gr, err := gzip.NewReader(f)
	if err != nil {
		return "", err
	}
	defer gr.Close()
	target := "/usr/local/bin/rrsync"
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return "", err
	}
	out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0755)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(out, gr); err != nil {
		out.Close()
		return "", err
	}
	if err := out.Close(); err != nil {
		return "", err
	}
	return target, nil
}

func syncAuthorizedKeysPath() string {
	home := os.Getenv("HOME")
	if home == "" {
		home = "/root"
	}
	return filepath.Join(home, ".ssh", "authorized_keys")
}

func syncPubkeyMarker(keyID string) string {
	return fmt.Sprintf("cluster-sync-key-id:%s", keyID)
}

func installSyncPubkey(payload InstallSyncPubkeyPayload) error {
	rrsyncPath, err := findRrsync()
	if err != nil {
		return err
	}
	allowedPath := filepath.Clean(strings.TrimSpace(payload.AllowedPath))
	if allowedPath == "" || !filepath.IsAbs(allowedPath) {
		return fmt.Errorf("invalid allowed path: %s", payload.AllowedPath)
	}
	if err := os.MkdirAll(allowedPath, 0755); err != nil {
		return fmt.Errorf("mkdir allowed path: %w", err)
	}
	pubkey := strings.TrimSpace(payload.PublicKey)
	if pubkey == "" {
		return fmt.Errorf("public key is empty")
	}
	keyID := strings.TrimSpace(payload.KeyID)
	if keyID == "" {
		return fmt.Errorf("key id is empty")
	}
	authKeysPath := syncAuthorizedKeysPath()
	sshDir := filepath.Dir(authKeysPath)
	if err := os.MkdirAll(sshDir, 0700); err != nil {
		return fmt.Errorf("mkdir ~/.ssh: %w", err)
	}

	marker := syncPubkeyMarker(keyID)
	command := fmt.Sprintf("%s %s", rrsyncPath, allowedPath)

	now := time.Now().Unix()
	existing, _ := os.ReadFile(authKeysPath)
	var kept []string
	for _, l := range strings.Split(string(existing), "\n") {
		trimmed := strings.TrimSpace(l)
		if trimmed == "" {
			continue
		}
		// 删除同 key_id 的旧记录以及已过期记录
		if strings.Contains(trimmed, marker) {
			continue
		}
		if idx := strings.Index(trimmed, "cluster-sync-key-id:"); idx != -1 {
			parts := strings.Split(trimmed[idx+len("cluster-sync-key-id:"):], ":")
			if len(parts) >= 3 && parts[1] == "expires" && parts[2] != "" {
				if exp, err := strconv.ParseInt(parts[2], 10, 64); err == nil && exp < now {
					continue
				}
			}
		}
		kept = append(kept, trimmed)
	}
	expires := payload.ExpiresAt
	if expires <= 0 {
		expires = now + 3600
	}
	line := fmt.Sprintf(
		"command=\"%s\",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-user-rc %s %s:expires:%d",
		command, pubkey, marker, expires,
	)
	kept = append(kept, line)
	content := strings.Join(kept, "\n") + "\n"
	if err := os.WriteFile(authKeysPath, []byte(content), 0600); err != nil {
		return fmt.Errorf("write authorized_keys: %w", err)
	}
	return nil
}

func removeSyncPubkey(payload RemoveSyncPubkeyPayload) error {
	keyID := strings.TrimSpace(payload.KeyID)
	authKeysPath := syncAuthorizedKeysPath()
	existing, err := os.ReadFile(authKeysPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("read authorized_keys: %w", err)
	}
	var marker string
	if keyID != "" {
		marker = syncPubkeyMarker(keyID)
	}
	pubkey := strings.TrimSpace(payload.PublicKey)
	var kept []string
	for _, l := range strings.Split(string(existing), "\n") {
		trimmed := strings.TrimSpace(l)
		if trimmed == "" {
			continue
		}
		if marker != "" && strings.Contains(trimmed, marker) {
			continue
		}
		if pubkey != "" && strings.Contains(trimmed, pubkey) {
			continue
		}
		kept = append(kept, trimmed)
	}
	if len(kept) == 0 {
		if err := os.Remove(authKeysPath); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove authorized_keys: %w", err)
		}
		return nil
	}
	content := strings.Join(kept, "\n") + "\n"
	if err := os.WriteFile(authKeysPath, []byte(content), 0600); err != nil {
		return fmt.Errorf("write authorized_keys: %w", err)
	}
	return nil
}
