package main

import (
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
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
	localFiles, fileCount, sizeBytes, err := collectResourceFiles(source, info)
	if err != nil {
		return "", err
	}
	incompleteReasons, err := sharedResourceIncompleteReasons(source)
	if err != nil {
		return "", err
	}
	resultDetail := map[string]any{
		"resource_id":        payload.ResourceID,
		"resource_type":      payload.ResourceType,
		"name":               payload.Name,
		"version":            payload.Version,
		"source_path":        source,
		"file_count":         fileCount,
		"size_bytes":         sizeBytes,
		"incomplete_reasons": incompleteReasons,
	}
	if strings.EqualFold(strings.TrimSpace(payload.Source), "huggingface") && strings.TrimSpace(payload.RepoID) != "" {
		remoteDetail, differences, verifyErr := verifyHuggingFaceManifest(payload, source, localFiles)
		for key, value := range remoteDetail {
			resultDetail[key] = value
		}
		incompleteReasons = append(incompleteReasons, differences...)
		resultDetail["incomplete_reasons"] = incompleteReasons
		if verifyErr != nil {
			resultDetail["remote_error"] = verifyErr.Error()
			result, _ := json.Marshal(resultDetail)
			return string(result), fmt.Errorf("resource verification failed: cannot verify Hugging Face repository via configured endpoint: %w", verifyErr)
		}
	}
	result, _ := json.Marshal(resultDetail)
	if fileCount == 0 {
		return string(result), fmt.Errorf("resource verification failed: no files found")
	}
	if len(incompleteReasons) > 0 {
		return string(result), fmt.Errorf("resource verification failed: incomplete download evidence found: %s", strings.Join(incompleteReasons, "; "))
	}
	return string(result), nil
}

func collectResourceFiles(source string, sourceInfo os.FileInfo) (map[string]int64, int64, int64, error) {
	files := map[string]int64{}
	if !sourceInfo.IsDir() {
		files[filepath.Base(source)] = sourceInfo.Size()
		return files, 1, sourceInfo.Size(), nil
	}
	var count, bytes int64
	err := filepath.WalkDir(source, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if rel != "." && (entry.Name() == ".hfd" || entry.Name() == ".cache" || entry.Name() == ".git") {
				return filepath.SkipDir
			}
			return nil
		}
		if rel == ".cluster-resource-id" {
			return nil
		}
		fileInfo, err := entry.Info()
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		files[rel] = fileInfo.Size()
		count++
		bytes += fileInfo.Size()
		return nil
	})
	return files, count, bytes, err
}

type hfRepoMetadata struct {
	SHA      string `json:"sha"`
	Siblings []struct {
		Path string `json:"rfilename"`
		Size int64  `json:"size"`
	} `json:"siblings"`
}

type hfTreeEntry struct {
	Type string `json:"type"`
	Path string `json:"path"`
	Size int64  `json:"size"`
}

func verifyHuggingFaceManifest(payload SharedResourceVerifyPayload, source string, localFiles map[string]int64) (map[string]any, []string, error) {
	endpoint := strings.TrimRight(strings.TrimSpace(payload.HFEndpoint), "/")
	if endpoint == "" {
		endpoint = "https://huggingface.co"
	}
	parsedEndpoint, err := url.Parse(endpoint)
	if err != nil || (parsedEndpoint.Scheme != "http" && parsedEndpoint.Scheme != "https") || parsedEndpoint.Host == "" {
		return nil, nil, fmt.Errorf("invalid HF endpoint %q", endpoint)
	}
	repoID := strings.Trim(strings.TrimSpace(payload.RepoID), "/")
	revision := strings.TrimSpace(payload.Revision)
	if revision == "" {
		revision = "main"
	}
	repoKind := "models"
	if strings.EqualFold(strings.TrimSpace(payload.RepoType), "dataset") {
		repoKind = "datasets"
	}
	apiBase := endpoint + "/api/" + repoKind + "/" + repoID
	metadataURL := apiBase
	if revision != "main" {
		metadataURL += "/revision/" + url.PathEscape(revision)
	}
	metadataURL += "?blobs=true"
	client := &http.Client{
		Timeout: 2 * time.Minute,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if !strings.EqualFold(parsedEndpoint.Hostname(), "huggingface.co") && strings.EqualFold(req.URL.Hostname(), "huggingface.co") {
				return fmt.Errorf("configured HF endpoint redirected repository API to huggingface.co: %s", req.URL.String())
			}
			if len(via) >= 10 {
				return fmt.Errorf("too many redirects")
			}
			return nil
		},
	}
	var remoteMetadata hfRepoMetadata
	if _, err := getHFJSON(client, metadataURL, payload.Token, &remoteMetadata); err != nil {
		if payload.AllowOfflineManifest {
			detail, expected, localErr := readLocalHFDownloadEvidence(source, localFiles)
			if localErr == nil {
				detail["hf_endpoint"] = endpoint
				detail["repo_id"] = repoID
				detail["revision"] = revision
				detail["verification_mode"] = "local_hfd_evidence"
				detail["remote_warning"] = err.Error()
				differences := compareResourceManifest(expected, localFiles)
				detail["difference_count"] = len(differences)
				return detail, differences, nil
			}
			return map[string]any{"hf_endpoint": endpoint, "repo_id": repoID, "revision": revision}, nil,
				fmt.Errorf("remote verification failed (%v) and local hfd evidence is invalid: %w", err, localErr)
		}
		return map[string]any{"hf_endpoint": endpoint, "repo_id": repoID, "revision": revision}, nil, err
	}

	detail := map[string]any{
		"hf_endpoint": endpoint,
		"repo_id":     repoID,
		"revision":    revision,
		"remote_sha":  remoteMetadata.SHA,
	}
	manifestPath := filepath.Join(source, ".hfd", "manifest")
	localMetadataPath := filepath.Join(source, ".hfd", "repo_metadata.json")
	expected, manifestErr := readHFDManifest(manifestPath)
	if manifestErr == nil {
		var localMetadata hfRepoMetadata
		metadataData, readErr := os.ReadFile(localMetadataPath)
		if readErr == nil && json.Unmarshal(metadataData, &localMetadata) == nil && localMetadata.SHA != "" {
			detail["downloaded_sha"] = localMetadata.SHA
			if remoteMetadata.SHA != "" && remoteMetadata.SHA != localMetadata.SHA {
				return detail, []string{fmt.Sprintf("remote revision changed after download: local sha %s, remote sha %s", localMetadata.SHA, remoteMetadata.SHA)}, nil
			}
		} else {
			expected, err = fetchHFRemoteManifest(client, apiBase, revision, payload.Token, remoteMetadata)
			if err != nil {
				return detail, nil, err
			}
		}
	} else if !os.IsNotExist(manifestErr) {
		return detail, nil, manifestErr
	} else {
		expected, err = fetchHFRemoteManifest(client, apiBase, revision, payload.Token, remoteMetadata)
		if err != nil {
			return detail, nil, err
		}
	}

	detail["expected_file_count"] = len(expected)
	var expectedBytes int64
	for _, size := range expected {
		expectedBytes += size
	}
	detail["expected_size_bytes"] = expectedBytes
	differences := compareResourceManifest(expected, localFiles)
	detail["difference_count"] = len(differences)
	return detail, differences, nil
}

func readLocalHFDownloadEvidence(source string, localFiles map[string]int64) (map[string]any, map[string]int64, error) {
	manifestPath := filepath.Join(source, ".hfd", "manifest")
	if expected, err := readHFDManifest(manifestPath); err == nil {
		metadataData, readErr := os.ReadFile(filepath.Join(source, ".hfd", "repo_metadata.json"))
		var metadata hfRepoMetadata
		if readErr != nil || json.Unmarshal(metadataData, &metadata) != nil || strings.TrimSpace(metadata.SHA) == "" {
			return nil, nil, fmt.Errorf(".hfd manifest has no valid repository SHA")
		}
		return localEvidenceDetail(expected, metadata.SHA, "hfd_manifest"), expected, nil
	} else if !os.IsNotExist(err) {
		return nil, nil, err
	}

	cacheRoot := filepath.Join(source, ".cache", "huggingface", "download")
	expected := map[string]int64{}
	downloadedSHA := ""
	err := filepath.WalkDir(cacheRoot, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".metadata") {
			return nil
		}
		rel, err := filepath.Rel(cacheRoot, strings.TrimSuffix(path, ".metadata"))
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		size, ok := localFiles[rel]
		if !ok {
			expected[rel] = -1
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		sha := strings.TrimSpace(strings.SplitN(string(data), "\n", 2)[0])
		if sha == "" {
			return fmt.Errorf("empty repository SHA in %s", rel)
		}
		if downloadedSHA == "" {
			downloadedSHA = sha
		} else if downloadedSHA != sha {
			return fmt.Errorf("mixed repository SHAs in local cache: %s and %s", downloadedSHA, sha)
		}
		expected[rel] = size
		return nil
	})
	if err != nil {
		return nil, nil, fmt.Errorf("read Hugging Face download metadata: %w", err)
	}
	if len(expected) == 0 || downloadedSHA == "" {
		return nil, nil, fmt.Errorf("no completed Hugging Face download metadata found")
	}
	return localEvidenceDetail(expected, downloadedSHA, "huggingface_cache_metadata"), expected, nil
}

func localEvidenceDetail(expected map[string]int64, downloadedSHA, evidence string) map[string]any {
	var expectedBytes int64
	for _, size := range expected {
		if size > 0 {
			expectedBytes += size
		}
	}
	return map[string]any{
		"downloaded_sha":      downloadedSHA,
		"local_evidence":      evidence,
		"expected_file_count": len(expected),
		"expected_size_bytes": expectedBytes,
	}
}

func getHFJSON(client *http.Client, requestURL, token string, target any) (http.Header, error) {
	req, err := http.NewRequest(http.MethodGet, requestURL, nil)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(token) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(token))
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("GET %s returned %s: %s", requestURL, resp.Status, strings.TrimSpace(string(body)))
	}
	if err := json.NewDecoder(resp.Body).Decode(target); err != nil {
		return nil, fmt.Errorf("decode %s: %w", requestURL, err)
	}
	return resp.Header, nil
}

func readHFDManifest(path string) (map[string]int64, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	manifest := map[string]int64{}
	for lineNumber, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		parts := strings.SplitN(line, "\t", 2)
		if len(parts) != 2 {
			return nil, fmt.Errorf("invalid hfd manifest line %d", lineNumber+1)
		}
		size, err := strconv.ParseInt(parts[0], 10, 64)
		if err != nil || size < 0 || strings.TrimSpace(parts[1]) == "" {
			return nil, fmt.Errorf("invalid hfd manifest line %d", lineNumber+1)
		}
		manifest[filepath.ToSlash(strings.TrimPrefix(parts[1], "./"))] = size
	}
	if len(manifest) == 0 {
		return nil, fmt.Errorf("hfd manifest is empty")
	}
	return manifest, nil
}

func fetchHFRemoteManifest(client *http.Client, apiBase, revision, token string, metadata hfRepoMetadata) (map[string]int64, error) {
	manifest := map[string]int64{}
	requestURL := apiBase + "/tree/" + url.PathEscape(revision) + "?recursive=true&expand=false"
	for requestURL != "" {
		var entries []hfTreeEntry
		headers, err := getHFJSON(client, requestURL, token, &entries)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if entry.Type == "file" {
				manifest[filepath.ToSlash(entry.Path)] = entry.Size
			}
		}
		requestURL = nextHFLink(headers.Get("Link"))
	}
	if len(manifest) == 0 {
		for _, sibling := range metadata.Siblings {
			if sibling.Path != "" {
				manifest[filepath.ToSlash(sibling.Path)] = sibling.Size
			}
		}
	}
	if len(manifest) == 0 {
		return nil, fmt.Errorf("remote repository returned an empty manifest")
	}
	return manifest, nil
}

func nextHFLink(linkHeader string) string {
	for _, part := range strings.Split(linkHeader, ",") {
		sections := strings.Split(part, ";")
		if len(sections) < 2 || !strings.Contains(strings.Join(sections[1:], ";"), `rel="next"`) {
			continue
		}
		return strings.TrimSuffix(strings.TrimPrefix(strings.TrimSpace(sections[0]), "<"), ">")
	}
	return ""
}

func compareResourceManifest(expected, local map[string]int64) []string {
	differences := []string{}
	add := func(message string) {
		if len(differences) < 20 {
			differences = append(differences, message)
		}
	}
	for path, expectedSize := range expected {
		localSize, ok := local[path]
		if !ok {
			add("missing remote file: " + path)
		} else if localSize != expectedSize {
			add(fmt.Sprintf("size mismatch: %s (local=%d remote=%d)", path, localSize, expectedSize))
		}
	}
	for path := range local {
		if _, ok := expected[path]; !ok {
			add("extra local file: " + path)
		}
	}
	return differences
}

func sharedResourceIncompleteReasons(source string) ([]string, error) {
	reasons := []string{}
	if info, err := os.Stat(filepath.Join(source, ".hfd", "needed")); err == nil && info.Size() > 0 {
		reasons = append(reasons, ".hfd/needed is not empty")
	} else if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("stat .hfd/needed: %w", err)
	}
	if info, err := os.Stat(filepath.Join(source, ".hfd", "failed")); err == nil && info.Size() > 0 {
		reasons = append(reasons, ".hfd/failed is not empty")
	} else if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("stat .hfd/failed: %w", err)
	}
	err := filepath.WalkDir(source, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			name := entry.Name()
			if name == ".git" || name == ".cache" {
				return filepath.SkipDir
			}
			return nil
		}
		name := entry.Name()
		if strings.HasSuffix(name, ".aria2") || strings.HasSuffix(name, ".incomplete") {
			rel, err := filepath.Rel(source, path)
			if err != nil {
				rel = path
			}
			reasons = append(reasons, "partial file remains: "+rel)
			if len(reasons) >= 20 {
				return filepath.SkipAll
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return reasons, nil
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

// attemptDirectContainerSync 尝试在容器内直接运行 rsync，
// 避免在宿主机（可能是 tmpfs 的 /tmp）上暂存大文件导致空间不足。
// 要求：容器内已安装 rsync，且不使用 JumpHost（容器内 ProxyCommand 配置复杂）。
func attemptDirectContainerSync(payload DataSyncPayload, onProgress func(SyncProgress)) (string, error) {
	// 检查容器内是否安装了 rsync
	if _, err := runCommandCombined("incus", "exec", payload.ContainerName, "--", "which", "rsync"); err != nil {
		return "", fmt.Errorf("rsync not available in container: %w", err)
	}
	endpoint := payload.SourceEndpoint
	// 暂不支持 JumpHost 直接同步（容器内的跳板机 SSH 配置过于复杂）
	if strings.TrimSpace(endpoint.JumpHost) != "" {
		return "", fmt.Errorf("jump host not supported for direct container sync")
	}
	host := strings.TrimSpace(endpoint.Host)
	user := strings.TrimSpace(endpoint.User)
	if user == "" {
		user = "root"
	}
	port := endpoint.Port
	if port <= 0 {
		port = 22
	}
	// 将 SSH 私钥推入容器内临时路径
	containerKeyPath := ""
	key := strings.TrimSpace(endpoint.PrivateKey)
	if key != "" {
		f, err := os.CreateTemp("", "cluster-sync-key-*")
		if err != nil {
			return "", fmt.Errorf("create temp key file: %w", err)
		}
		hostKeyPath := f.Name()
		defer os.Remove(hostKeyPath)
		// OpenSSH requires PEM key to end with newline
		if _, err := f.WriteString(key + "\n"); err != nil {
			f.Close()
			return "", fmt.Errorf("write temp key file: %w", err)
		}
		if err := f.Chmod(0600); err != nil {
			f.Close()
			return "", fmt.Errorf("chmod temp key file: %w", err)
		}
		f.Close()
		containerKeyPath = "/tmp/cluster-sync-key-direct"
		if _, err := runCommandCombined("incus", "file", "push", hostKeyPath,
			payload.ContainerName+containerKeyPath); err != nil {
			return "", fmt.Errorf("push key to container: %w", err)
		}
		defer runCommandCombined("incus", "exec", payload.ContainerName, "--", "rm", "-f", containerKeyPath) //nolint:errcheck
		if _, err := runCommandCombined("incus", "exec", payload.ContainerName, "--",
			"chmod", "600", containerKeyPath); err != nil {
			return "", fmt.Errorf("chmod key in container: %w", err)
		}
	}
	// 构建容器内 rsync 使用的 ssh 命令（容器无宿主机的 known_hosts，使用 /dev/null）
	sshCmd := fmt.Sprintf(
		"ssh -p %d -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
		port,
	)
	if containerKeyPath != "" {
		sshCmd += " -i " + containerKeyPath
	}
	// 构建远端路径（支持 rrsync restricted 模式）
	remotePath := strings.TrimSpace(payload.SourcePath)
	if endpoint.Restricted && strings.TrimSpace(endpoint.AllowedPath) != "" {
		rel, err := makeRestrictedRsyncPath(endpoint.AllowedPath, payload.SourcePath)
		if err != nil {
			return "", err
		}
		remotePath = rel
		if remotePath == "." {
			remotePath = "./"
		} else if !strings.HasSuffix(remotePath, "/") {
			remotePath += "/"
		}
	} else {
		remotePath = strings.TrimSuffix(remotePath, "/") + "/"
	}
	if strings.ContainsAny(remotePath, "\r\n") {
		return "", fmt.Errorf("source path contains invalid characters")
	}
	rsyncArgs := []string{"-a", "--info=progress2"}
	if payload.Delete {
		rsyncArgs = append(rsyncArgs, "--delete")
	}
	if payload.IgnoreExisting {
		rsyncArgs = append(rsyncArgs, "--ignore-existing")
	}
	if payload.Update {
		rsyncArgs = append(rsyncArgs, "--update")
	}
	if payload.BandwidthLimit > 0 {
		// BandwidthLimit 单位为 Mbps（Megabits/s），rsync --bwlimit 单位为 KB/s
		// 1 Mbps = 125 KB/s
		rsyncArgs = append(rsyncArgs, "--bwlimit="+strconv.Itoa(payload.BandwidthLimit*125))
	}
	rsyncArgs = append(rsyncArgs, "-e", sshCmd)
	rsyncArgs = append(rsyncArgs, fmt.Sprintf("%s@%s:%s", user, host, remotePath))
	rsyncArgs = append(rsyncArgs, strings.TrimSuffix(payload.TargetPath, "/")+"/")
	// 在容器内创建目标目录
	if _, err := runCommandCombined("incus", "exec", payload.ContainerName, "--",
		"mkdir", "-p", payload.TargetPath); err != nil {
		return "", fmt.Errorf("mkdir in container: %w", err)
	}
	// 在容器内运行 rsync
	incusArgs := append([]string{"exec", payload.ContainerName, "--", "rsync"}, rsyncArgs...)
	return runCommandWithProgress(60*time.Minute, onProgress, "incus", incusArgs...)
}

func executeContainerDataSync(payload DataSyncPayload, dataPath string, onProgress func(SyncProgress)) (string, error) {
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
	// 若目录不存在则尝试自动创建（/mnt/data 通常是磁盘挂载）
	tmpParent := "/mnt/data/tmp"
	if _, err := os.Stat(tmpParent); os.IsNotExist(err) {
		if mkErr := os.MkdirAll(tmpParent, 0755); mkErr != nil {
			tmpParent = "" // 回退到系统默认临时目录
		}
	}
	tmpDir, err := os.MkdirTemp(tmpParent, "cluster-container-sync-*")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tmpDir)

	fileTransferTimeout := 60 * time.Minute
	switch mode {
	case "storage_to_container":
		// 优先尝试在容器内直接运行 rsync，数据从存储节点直达容器，
		// 完全绕过宿主机暂存，避免宿主机 /tmp (tmpfs) 空间不足的问题。
		if payload.SourceEndpoint.Host != "" {
			if output, err := attemptDirectContainerSync(payload, onProgress); err == nil {
				return output, nil
			}
			// 直接同步不可用（如容器内无 rsync 或使用了 JumpHost），回退到暂存方案
		}
		// 暂存方案：先下载到本地 tmpDir 再通过 incus file push 推入容器
		staging := filepath.Join(tmpDir, "payload")
		if err := os.MkdirAll(staging, 0755); err != nil {
			return "", err
		}
		stagePayload := payload
		stagePayload.Mode = ""
		stagePayload.TargetPath = staging
		stagePayload.TargetEndpoint = DataSyncSSHEndpoint{}
		output, err := executeDataSync(stagePayload, dataPath, onProgress)
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
				out, err = runCommandCombinedTimeout(fileTransferTimeout, "incus", "file", "push", "-r", "-p", srcPath, dstTarget)
			} else {
				out, err = runCommandCombinedTimeout(fileTransferTimeout, "incus", "file", "push", "-p", srcPath, dstTarget)
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
		pullOutput, err := runCommandCombinedTimeout(fileTransferTimeout, "incus", "file", "pull", "-r", source, staging)
		if err != nil {
			return pullOutput, err
		}
		stagePayload := payload
		stagePayload.Mode = ""
		stagePayload.SourcePath = staging
		stagePayload.SourceEndpoint = DataSyncSSHEndpoint{}
		output, err := executeDataSync(stagePayload, dataPath, onProgress)
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
	// OpenSSH requires PEM key to end with newline; TrimSpace removes it, so add it back
	if _, err := f.WriteString(key + "\n"); err != nil {
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

func executeDataSync(payload DataSyncPayload, dataPath string, onProgress func(SyncProgress)) (string, error) {
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
	args := []string{"-a", "--info=progress2"}
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
		// BandwidthLimit 单位为 Mbps（Megabits/s），rsync --bwlimit 单位为 KB/s
		// 1 Mbps = 125 KB/s
		args = append(args, "--bwlimit="+strconv.Itoa(payload.BandwidthLimit*125))
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
	output, err := runCommandWithProgress(60*time.Minute, onProgress, "rsync", args...)
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
