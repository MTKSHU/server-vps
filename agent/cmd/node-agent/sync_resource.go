package main

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// executeSyncSharedResource 将公共数据集/模型从存储节点 rsync 到本地缓存目录。
//
// 路径由后端计算好写入 payload，agent 直接 mkdir + rsync，无需自行推导。
func executeSyncSharedResource(payload SyncSharedResourcePayload) (string, error) {
	if payload.LocalCachePath == "" {
		return "", fmt.Errorf("local_cache_path is empty")
	}
	if payload.SourceHost == "" {
		return "", fmt.Errorf("source_host is empty")
	}
	if payload.SourcePath == "" {
		return "", fmt.Errorf("source_path is empty")
	}

	// 确保本地缓存目录存在（与 zfs.go / directory.go 中的 MkdirAll 惯例一致）
	if err := os.MkdirAll(payload.LocalCachePath, 0755); err != nil {
		return "", fmt.Errorf("mkdir %s: %w", payload.LocalCachePath, err)
	}

	// 如果 payload 携带私钥内容，写入临时文件
	sshOpts := "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"
	var keyFilePath string
	if strings.TrimSpace(payload.SourcePrivateKey) != "" {
		tmp, err := os.CreateTemp("", "sync-resource-key-*")
		if err != nil {
			return "", fmt.Errorf("create temp key file: %w", err)
		}
		defer os.Remove(tmp.Name())
		if err := tmp.Chmod(0600); err != nil {
			return "", fmt.Errorf("chmod temp key file: %w", err)
		}
		if _, err := tmp.WriteString(payload.SourcePrivateKey); err != nil {
			return "", fmt.Errorf("write temp key file: %w", err)
		}
		if err := tmp.Close(); err != nil {
			return "", fmt.Errorf("close temp key file: %w", err)
		}
		keyFilePath = tmp.Name()
		sshOpts += " -i " + keyFilePath
	}

	port := payload.SourcePort
	if port <= 0 {
		port = 22
	}
	sshOpts += fmt.Sprintf(" -p %d", port)

	user := strings.TrimSpace(payload.SourceUser)
	if user == "" {
		user = "root"
	}

	// rsync 拉取：source_path/ → local_cache_path/（--delete 保持缓存与源一致）
	sourcePath := strings.TrimRight(payload.SourcePath, "/") + "/"
	localPath := strings.TrimRight(payload.LocalCachePath, "/") + "/"
	source := fmt.Sprintf("%s@%s:%s", user, payload.SourceHost, sourcePath)

	output, err := runCommandCombinedTimeout(
		4*time.Hour,
		"rsync", "-az", "--delete", "-e", sshOpts, source, localPath,
	)
	return strings.TrimSpace(output), err
}

// executeApplyResourceMounts 将容器的资源挂载设备热更新为本地缓存路径，同时支持更换挂载点。
func executeApplyResourceMounts(payload ApplyResourceMountsPayload, dataPath string) (string, error) {
	var sb strings.Builder
	appliedCount := 0
	for index, upd := range payload.MountUpdates {
		if upd.NewSource == "" || upd.NewTarget == "" {
			continue
		}
		if err := ensureMountSource(upd.NewSource, dataPath); err != nil {
			return sb.String(), fmt.Errorf("mount source not available for %s -> %s: %w", upd.NewSource, upd.NewTarget, err)
		}
		// 按旧 target 路径找到并删除旧设备（支持 target 内容变更的情况）
		oldTarget := upd.OldTarget
		if oldTarget == "" {
			oldTarget = upd.NewTarget
		}
		for _, devName := range diskDeviceNamesForPath(payload.Name, oldTarget) {
			_, _ = runCommandCombined("incus", "config", "device", "remove", payload.Name, devName)
		}
		// 添加新设备（新 source 、新 target）
		args := []string{"disk", "source=" + upd.NewSource, "path=" + upd.NewTarget}
		if upd.Readonly {
			args = append(args, "readonly=true")
		}
		devName := safeDeviceName("disk", index, upd.NewTarget)
		_, err := runCommandCombined("incus", append([]string{"config", "device", "add", payload.Name, devName}, args...)...)
		if err != nil {
			return sb.String(), fmt.Errorf("apply mount %s -> %s: %w", upd.NewSource, upd.NewTarget, err)
		}
		fmt.Fprintf(&sb, "applied %s -> %s\n", upd.NewSource, upd.NewTarget)
		appliedCount++
	}
	if appliedCount == 0 && len(payload.MountUpdates) > 0 {
		return sb.String(), fmt.Errorf("no mount updates were applied (all %d entries skipped)", len(payload.MountUpdates))
	}
	return strings.TrimSpace(sb.String()), nil
}
