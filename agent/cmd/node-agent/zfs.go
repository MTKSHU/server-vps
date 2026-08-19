package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type zfsDatasetResult struct {
	UserID         int    `json:"user_id"`
	Username       string `json:"username"`
	DatasetName    string `json:"dataset_name"`
	Mountpoint     string `json:"mountpoint"`
	QuotaGB        int    `json:"quota_gb"`
	Migrated       bool   `json:"migrated"`
	BackupPath     string `json:"backup_path,omitempty"`
	NFSShareID     int    `json:"nfs_share_id,omitempty"`
	NFSExportPath  string `json:"nfs_export_path,omitempty"`
	NFSShareStatus string `json:"nfs_share_status,omitempty"`
}

type trueNASNFSShare struct {
	ID           int      `json:"id"`
	Path         string   `json:"path"`
	Networks     []string `json:"networks"`
	Hosts        []string `json:"hosts"`
	ReadOnly     bool     `json:"ro"`
	MapRootUser  *string  `json:"maproot_user"`
	MapRootGroup *string  `json:"maproot_group"`
	MapAllUser   *string  `json:"mapall_user"`
	MapAllGroup  *string  `json:"mapall_group"`
	Security     []string `json:"security"`
}

func executeEnsureUserZFSDataset(payload UserZFSDatasetPayload) (string, error) {
	if _, err := exec.LookPath("zfs"); err != nil {
		return "", fmt.Errorf("zfs command not found: %w", err)
	}
	mountpoint := filepath.Clean(strings.TrimSpace(payload.Mountpoint))
	if err := validateAbsolutePath(mountpoint); err != nil {
		return "", err
	}
	if !regexp.MustCompile(`^[a-z][a-z0-9_.-]{2,31}$`).MatchString(payload.Username) {
		return "", fmt.Errorf("username %q is not safe for dataset operations", payload.Username)
	}
	dataset := strings.TrimSpace(payload.DatasetName)
	var err error
	if dataset == "" {
		dataset, err = deriveZFSDatasetName(mountpoint)
		if err != nil {
			return "", err
		}
	}
	if err := validateZFSDatasetName(dataset); err != nil {
		return "", err
	}

	exists := zfsDatasetExists(dataset)
	currentMountpoint := ""
	if exists {
		currentMountpoint = zfsDatasetProperty(dataset, "mountpoint")
	}

	result := zfsDatasetResult{
		UserID:      payload.UserID,
		Username:    payload.Username,
		DatasetName: dataset,
		Mountpoint:  mountpoint,
		QuotaGB:     payload.QuotaGB,
	}

	needsMigration := !exists && directoryHasEntries(mountpoint)
	if needsMigration {
		backupPath, err := migrateDirectoryToZFSDataset(dataset, mountpoint)
		if err != nil {
			return "", err
		}
		result.Migrated = true
		result.BackupPath = backupPath
		exists = true
		currentMountpoint = mountpoint
	}

	if !exists {
		if err := os.MkdirAll(filepath.Dir(mountpoint), 0755); err != nil {
			return "", fmt.Errorf("mkdir parent: %w", err)
		}
		// 创建数据集时不显式设置mountpoint，让它从父数据集继承
		if output, err := runCommandCombined("zfs", "create", "-p", dataset); err != nil {
			return output, err
		}
		// 创建后获取继承的挂载点
		currentMountpoint = zfsDatasetProperty(dataset, "mountpoint")
	}
	// 只有当中中挂载点与期望的不一致，且期望的挂载点看起来合理时，才显式设置
	// 否则保持继承（让ZFS自动管理）
	if currentMountpoint != mountpoint && strings.HasPrefix(mountpoint, "/") {
		// 检查继承的挂载点是否有效（非空且不是"-"）
		if currentMountpoint == "" || currentMountpoint == "-" || !strings.HasPrefix(currentMountpoint, "/") {
			if output, err := runCommandCombined("zfs", "set", "mountpoint="+mountpoint, dataset); err != nil {
				return output, err
			}
		}
	}
	if payload.QuotaGB > 0 {
		if output, err := runCommandCombined("zfs", "set", "quota="+strconv.Itoa(payload.QuotaGB)+"G", dataset); err != nil {
			return output, err
		}
		// refquota makes statfs/df report the user's dataset limit instead of
		// the capacity of the entire parent pool.
		if output, err := runCommandCombined("zfs", "set", "refquota="+strconv.Itoa(payload.QuotaGB)+"G", dataset); err != nil {
			return output, err
		}
	} else {
		if output, err := runCommandCombined("zfs", "inherit", "quota", dataset); err != nil {
			return output, err
		}
		if output, err := runCommandCombined("zfs", "inherit", "refquota", dataset); err != nil {
			return output, err
		}
	}
	if err := os.MkdirAll(mountpoint, 0750); err != nil {
		return "", fmt.Errorf("mkdir mountpoint: %w", err)
	}
	if payload.UID >= 0 && payload.GID >= 0 {
		_ = os.Chown(mountpoint, payload.UID, payload.GID)
	}
	if payload.Sentinel != "" && payload.SentinelSignature != "" {
		if !safeSentinel.MatchString(payload.Sentinel) || !safeSentinelSignature.MatchString(payload.SentinelSignature) {
			return "", fmt.Errorf("invalid NFS sentinel configuration")
		}
		sentinelPath := filepath.Join(mountpoint, payload.Sentinel)
		if err := os.WriteFile(sentinelPath, []byte(payload.SentinelSignature+"\n"), 0o444); err != nil {
			return "", fmt.Errorf("write NFS sentinel: %w", err)
		}
		_ = os.Chown(sentinelPath, payload.UID, payload.GID)
		_ = os.Chmod(sentinelPath, 0o444)
	}
	if mode := parseFileMode(payload.Mode); mode != 0 {
		_ = os.Chmod(mountpoint, mode)
	}
	result.NFSExportPath = mountpoint
	result.NFSShareStatus = "manual"
	if payload.EnsureNFSShare {
		shareID, err := ensureTrueNASUserNFSShare(mountpoint, payload.NFSShareTemplatePath, payload.Username)
		if err != nil {
			return "", err
		}
		result.NFSShareID = shareID
		result.NFSShareStatus = "applied"
	}
	data, _ := json.Marshal(result)
	return string(data), nil
}

func optionalStringValue(value *string) string {
	if value == nil {
		return ""
	}
	return strings.TrimSpace(*value)
}

func validateTrueNASUserNFSShare(share *trueNASNFSShare, path string) error {
	if share == nil {
		return fmt.Errorf("TrueNAS NFS share %s was not found", path)
	}
	if len(share.Hosts) == 0 && len(share.Networks) == 0 {
		return fmt.Errorf("TrueNAS NFS share %s has no hosts or networks restriction", path)
	}
	if share.ReadOnly {
		return fmt.Errorf("TrueNAS NFS share %s is read-only", path)
	}
	if strings.EqualFold(optionalStringValue(share.MapRootUser), "root") || strings.EqualFold(optionalStringValue(share.MapAllUser), "root") {
		return fmt.Errorf("TrueNAS NFS share %s permits root mapping", path)
	}
	return nil
}

func ensureTrueNASUserNFSShare(mountpoint, templatePath, username string) (int, error) {
	if _, err := exec.LookPath("midclt"); err != nil {
		return 0, fmt.Errorf("TrueNAS NFS auto-share requires midclt: %w", err)
	}
	templatePath = filepath.Clean(strings.TrimSpace(templatePath))
	if err := validateAbsolutePath(templatePath); err != nil {
		return 0, fmt.Errorf("invalid NFS share template path: %w", err)
	}
	output, err := runCommandCombined("midclt", "call", "sharing.nfs.query")
	if err != nil {
		return 0, fmt.Errorf("query TrueNAS NFS shares: %w (%s)", err, strings.TrimSpace(output))
	}
	shares := []trueNASNFSShare{}
	if err := json.Unmarshal([]byte(output), &shares); err != nil {
		return 0, fmt.Errorf("decode TrueNAS NFS shares: %w", err)
	}
	var template *trueNASNFSShare
	var existing *trueNASNFSShare
	for index := range shares {
		sharePath := filepath.Clean(shares[index].Path)
		if sharePath == filepath.Clean(mountpoint) {
			existing = &shares[index]
		}
		if sharePath == templatePath {
			template = &shares[index]
		}
	}
	// Once a per-user share exists, it is the authoritative object. Requiring
	// the bootstrap template on every idempotent reconciliation would break all
	// existing users after the template is intentionally removed.
	if existing != nil {
		if err := validateTrueNASUserNFSShare(existing, mountpoint); err != nil {
			return 0, err
		}
		return existing.ID, nil
	}
	if template == nil {
		return 0, fmt.Errorf("TrueNAS NFS template share %s was not found", templatePath)
	}
	if err := validateTrueNASUserNFSShare(template, templatePath); err != nil {
		return 0, fmt.Errorf("invalid TrueNAS NFS template share %s: %w", templatePath, err)
	}
	request := map[string]any{
		"path":          mountpoint,
		"comment":       "server-vps user " + username,
		"networks":      template.Networks,
		"hosts":         template.Hosts,
		"ro":            false,
		"maproot_user":  template.MapRootUser,
		"maproot_group": template.MapRootGroup,
		"mapall_user":   template.MapAllUser,
		"mapall_group":  template.MapAllGroup,
		"security":      template.Security,
		"enabled":       true,
	}
	encoded, err := json.Marshal(request)
	if err != nil {
		return 0, err
	}
	output, err = runCommandCombined("midclt", "call", "sharing.nfs.create", string(encoded))
	if err != nil {
		return 0, fmt.Errorf("create TrueNAS NFS share for %s: %w (%s)", username, err, strings.TrimSpace(output))
	}
	created := trueNASNFSShare{}
	if err := json.Unmarshal([]byte(output), &created); err != nil || created.ID <= 0 {
		return 0, fmt.Errorf("decode created TrueNAS NFS share: %w", err)
	}
	return created.ID, nil
}

func validateAbsolutePath(path string) error {
	if path == "" || !strings.HasPrefix(path, "/") || path == "/" || strings.Contains(path, "\x00") {
		return fmt.Errorf("invalid absolute path: %q", path)
	}
	for _, part := range strings.Split(path, "/") {
		if part == ".." {
			return fmt.Errorf("path escapes parent: %q", path)
		}
	}
	return nil
}

func validateZFSDatasetName(dataset string) error {
	if dataset == "" || strings.HasPrefix(dataset, "/") || strings.Contains(dataset, "\x00") {
		return fmt.Errorf("invalid zfs dataset name: %q", dataset)
	}
	segmentPattern := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:%-]{0,127}$`)
	for _, segment := range strings.Split(dataset, "/") {
		if !segmentPattern.MatchString(segment) {
			return fmt.Errorf("invalid zfs dataset segment %q", segment)
		}
	}
	return nil
}

func deriveZFSDatasetName(mountpoint string) (string, error) {
	output, err := runCommandCombined("zfs", "list", "-H", "-o", "name,mountpoint")
	if err != nil {
		return output, err
	}
	bestName := ""
	bestMount := ""
	for _, line := range strings.Split(output, "\n") {
		parts := strings.Split(line, "\t")
		if len(parts) < 2 {
			continue
		}
		name := strings.TrimSpace(parts[0])
		mp := filepath.Clean(strings.TrimSpace(parts[1]))
		if mp == "" || mp == "-" || !strings.HasPrefix(mp, "/") {
			continue
		}
		if mountpoint == mp || strings.HasPrefix(mountpoint, strings.TrimRight(mp, "/")+"/") {
			if len(mp) > len(bestMount) {
				bestName = name
				bestMount = mp
			}
		}
	}
	if bestName == "" {
		return "", fmt.Errorf("no parent zfs dataset mounted above %s", mountpoint)
	}
	relative := strings.Trim(strings.TrimPrefix(mountpoint, strings.TrimRight(bestMount, "/")), "/")
	if relative == "" {
		return bestName, nil
	}
	return bestName + "/" + strings.ReplaceAll(relative, string(os.PathSeparator), "/"), nil
}

func zfsDatasetExists(dataset string) bool {
	_, err := runCommandCombined("zfs", "list", "-H", "-o", "name", dataset)
	return err == nil
}

func zfsDatasetProperty(dataset string, property string) string {
	output := runCommand("zfs", "get", "-H", "-o", "value", property, dataset)
	return strings.TrimSpace(output)
}

func zfsDatasetForMountpoint(mountpoint string) (string, error) {
	output, err := runCommandCombined("zfs", "list", "-H", "-o", "name,mountpoint")
	if err != nil {
		return output, err
	}
	cleanMountpoint := filepath.Clean(mountpoint)
	for _, line := range strings.Split(output, "\n") {
		parts := strings.Split(line, "\t")
		if len(parts) < 2 {
			continue
		}
		name := strings.TrimSpace(parts[0])
		mp := filepath.Clean(strings.TrimSpace(parts[1]))
		if name != "" && mp == cleanMountpoint {
			return name, nil
		}
	}
	return "", nil
}

func directoryHasEntries(path string) bool {
	entries, err := os.ReadDir(path)
	return err == nil && len(entries) > 0
}

func migrateDirectoryToZFSDataset(dataset string, mountpoint string) (string, error) {
	stamp := time.Now().Format("20060102-150405")
	tempMount := mountpoint + ".zfs-migrate-" + stamp
	backupPath := mountpoint + ".bak-" + stamp
	if err := os.MkdirAll(tempMount, 0750); err != nil {
		return "", fmt.Errorf("mkdir temp mountpoint: %w", err)
	}
	if output, err := runCommandCombined("zfs", "create", "-p", "-o", "mountpoint="+tempMount, dataset); err != nil {
		return output, err
	}
	source := strings.TrimRight(mountpoint, "/") + "/"
	target := strings.TrimRight(tempMount, "/") + "/"
	if output, err := runCommandCombined("rsync", "-aHAX", "--numeric-ids", source, target); err != nil {
		return output, err
	}
	if output, err := runCommandCombined("rsync", "-aHAX", "--numeric-ids", "--delete", source, target); err != nil {
		return output, err
	}
	if err := os.Rename(mountpoint, backupPath); err != nil {
		return "", fmt.Errorf("backup old user directory: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(mountpoint), 0755); err != nil {
		return "", fmt.Errorf("mkdir final parent: %w", err)
	}
	if output, err := runCommandCombined("zfs", "set", "mountpoint="+mountpoint, dataset); err != nil {
		return output, err
	}
	return backupPath, nil
}

func executeRemoveUserZFSDataset(payload UserZFSDatasetRemovePayload) (string, error) {
	dataset := strings.TrimSpace(payload.DatasetName)
	if dataset == "" {
		mountpoint := filepath.Clean(strings.TrimSpace(payload.Mountpoint))
		if err := validateAbsolutePath(mountpoint); err != nil {
			return "", err
		}
		if _, err := exec.LookPath("zfs"); err == nil {
			mountedDataset, err := zfsDatasetForMountpoint(mountpoint)
			if err != nil {
				return "", fmt.Errorf("lookup zfs dataset for %s: %w", mountpoint, err)
			}
			if mountedDataset != "" {
				return removeZFSDataset(mountedDataset)
			}
		}
		if _, err := os.Stat(mountpoint); os.IsNotExist(err) {
			return fmt.Sprintf("mountpoint %s does not exist, skipped", mountpoint), nil
		}
		if err := os.RemoveAll(mountpoint); err != nil {
			return "", fmt.Errorf("remove mountpoint %s: %w", mountpoint, err)
		}
		return fmt.Sprintf("removed mountpoint %s", mountpoint), nil
	}
	return removeZFSDataset(dataset)
}

func removeZFSDataset(dataset string) (string, error) {
	if _, err := exec.LookPath("zfs"); err != nil {
		return "", fmt.Errorf("zfs command not found: %w", err)
	}
	if err := validateZFSDatasetName(dataset); err != nil {
		return "", err
	}
	if !zfsDatasetExists(dataset) {
		return fmt.Sprintf("dataset %s does not exist, skipped", dataset), nil
	}
	// 卸载 dataset（如果已挂载）
	if output, err := runCommandCombined("zfs", "unmount", dataset); err != nil {
		// 如果未挂载，unmount 会失败，但可以继续
		_ = output
	}
	// 销毁 dataset
	output, err := runCommandCombined("zfs", "destroy", dataset)
	if err != nil {
		return output, fmt.Errorf("zfs destroy %s: %w", dataset, err)
	}
	return fmt.Sprintf("removed dataset %s", dataset), nil
}

func parseFileMode(value string) os.FileMode {
	value = strings.TrimSpace(value)
	if value == "" {
		return 0
	}
	parsed, err := strconv.ParseUint(value, 8, 32)
	if err != nil {
		return 0
	}
	return os.FileMode(parsed)
}
