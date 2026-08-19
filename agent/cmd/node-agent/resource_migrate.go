package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

func validateMigrationPath(path string, label string) (string, error) {
	clean := filepath.Clean(strings.TrimSpace(path))
	if clean == "." || !filepath.IsAbs(clean) {
		return "", fmt.Errorf("%s must be an absolute path", label)
	}
	if strings.Contains(clean, "\x00") {
		return "", fmt.Errorf("%s contains invalid NUL byte", label)
	}
	return clean, nil
}

func sameSymlinkTarget(linkPath string, targetPath string) bool {
	link, err := os.Readlink(linkPath)
	if err != nil {
		return false
	}
	if !filepath.IsAbs(link) {
		link = filepath.Join(filepath.Dir(linkPath), link)
	}
	return filepath.Clean(link) == filepath.Clean(targetPath)
}

func pathExists(path string) bool {
	_, err := os.Lstat(path)
	return err == nil
}

func executeMigrateSharedResourcePath(payload MigrateSharedResourcePathPayload) (string, error) {
	oldPath, err := validateMigrationPath(payload.OldPath, "old_path")
	if err != nil {
		return "", err
	}
	newPath, err := validateMigrationPath(payload.NewPath, "new_path")
	if err != nil {
		return "", err
	}
	if oldPath == newPath {
		return "", fmt.Errorf("old_path and new_path are identical")
	}
	if strings.HasPrefix(newPath, oldPath+string(os.PathSeparator)) {
		return "", fmt.Errorf("new_path cannot be inside old_path")
	}
	if strings.HasPrefix(oldPath, newPath+string(os.PathSeparator)) {
		return "", fmt.Errorf("old_path cannot be inside new_path")
	}

	result := map[string]any{
		"resource_id":     payload.ResourceID,
		"old_path":        oldPath,
		"new_path":        newPath,
		"old_source_path": payload.OldSourcePath,
		"new_source_path": payload.NewSourcePath,
		"create_symlink":  payload.CreateSymlink,
	}

	oldInfo, oldErr := os.Lstat(oldPath)
	newExists := pathExists(newPath)
	if oldErr != nil {
		if os.IsNotExist(oldErr) && newExists {
			result["status"] = "already_migrated"
			out, _ := json.Marshal(result)
			return string(out), nil
		}
		return "", fmt.Errorf("stat old_path: %w", oldErr)
	}
	if oldInfo.Mode()&os.ModeSymlink != 0 && sameSymlinkTarget(oldPath, newPath) {
		result["status"] = "already_migrated"
		out, _ := json.Marshal(result)
		return string(out), nil
	}
	if newExists {
		return "", fmt.Errorf("new_path already exists: %s", newPath)
	}
	if err := os.MkdirAll(filepath.Dir(newPath), 0755); err != nil {
		return "", fmt.Errorf("mkdir new parent: %w", err)
	}

	result["method"] = "rename"
	if err := os.Rename(oldPath, newPath); err != nil {
		linkErr, ok := err.(*os.LinkError)
		if !ok || linkErr.Err != syscall.EXDEV {
			return "", fmt.Errorf("rename resource path: %w", err)
		}
		result["method"] = "copy"
		if err := copyPath(oldPath, newPath); err != nil {
			return "", fmt.Errorf("copy resource path across filesystems: %w", err)
		}
		if err := os.RemoveAll(oldPath); err != nil {
			return "", fmt.Errorf("remove old path after copy: %w", err)
		}
	}

	if payload.CreateSymlink {
		if err := os.MkdirAll(filepath.Dir(oldPath), 0755); err != nil {
			return "", fmt.Errorf("mkdir old parent for symlink: %w", err)
		}
		if !pathExists(oldPath) {
			if err := os.Symlink(newPath, oldPath); err != nil {
				return "", fmt.Errorf("create compatibility symlink: %w", err)
			}
			result["symlink"] = oldPath
		}
	}

	// 旧布局是 {base}/{name}/{provider}，rename 后 {base}/{name} 可能变成空目录。
	// os.Remove 只在目录为空时才会成功，不会误删仍有内容的目录（如未创建 symlink 的其它资源共享的
	// name 目录，或 legacy_custom 布局下 oldPath 本身就是 base 目录的情况）。
	if !pathExists(oldPath) {
		if legacyParent := filepath.Dir(oldPath); os.Remove(legacyParent) == nil {
			result["removed_empty_legacy_dir"] = legacyParent
		}
	}

	result["status"] = "migrated"
	out, _ := json.Marshal(result)
	return string(out), nil
}
