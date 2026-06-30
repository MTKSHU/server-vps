package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type userDirectoryEntry struct {
	Name      string `json:"name"`
	Type      string `json:"type"`
	SizeBytes int64  `json:"size_bytes"`
	Mtime     int64  `json:"mtime"`
	Mode      string `json:"mode"`
}

func pathWithinRoot(path string, root string) bool {
	relative, err := filepath.Rel(filepath.Clean(root), filepath.Clean(path))
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(os.PathSeparator))
}

func executeUserDirectoryScan(payload UserDirectoryScanPayload, dataPath string) (string, error) {
	root, err := cleanSyncPath(payload.RootPath)
	if err != nil {
		return "", err
	}
	target, err := cleanSyncPath(payload.Path)
	if err != nil {
		return "", err
	}
	if !pathWithinRoot(target, root) || !isManagedMountSource(root, dataPath) {
		return "", fmt.Errorf("directory scan path escapes managed user root")
	}
	if filepath.Base(filepath.Dir(root)) != "users" || filepath.Base(root) != payload.Username {
		return "", fmt.Errorf("directory scan root does not match user home layout")
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	resolvedTarget, err := filepath.EvalSymlinks(target)
	if err != nil {
		return "", err
	}
	if !pathWithinRoot(resolvedTarget, resolvedRoot) || !isManagedMountSource(resolvedRoot, dataPath) {
		return "", fmt.Errorf("directory scan symlink escapes managed user root")
	}
	target = resolvedTarget
	items, err := os.ReadDir(target)
	if err != nil {
		return "", err
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].IsDir() != items[j].IsDir() {
			return items[i].IsDir()
		}
		return items[i].Name() < items[j].Name()
	})
	limit := payload.Limit
	if limit < 1 || limit > 1000 {
		limit = 500
	}
	entries := make([]userDirectoryEntry, 0, min(len(items), limit))
	for _, item := range items {
		if len(entries) >= limit {
			break
		}
		info, err := item.Info()
		if err != nil {
			return "", err
		}
		entryType := "file"
		if item.IsDir() {
			entryType = "directory"
		} else if info.Mode()&os.ModeSymlink != 0 {
			entryType = "symlink"
		}
		entries = append(entries, userDirectoryEntry{
			Name: item.Name(), Type: entryType, SizeBytes: info.Size(),
			Mtime: info.ModTime().Unix(), Mode: info.Mode().String(),
		})
	}
	var fileCount int64
	var sizeBytes int64
	err = filepath.WalkDir(target, func(path string, entry os.DirEntry, walkErr error) error {
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
	result, err := json.Marshal(map[string]any{
		"user_id": payload.UserID, "username": payload.Username,
		"relative_path": payload.RelativePath, "path": target,
		"entries": entries, "truncated": len(items) > limit,
		"file_count": fileCount, "size_bytes": sizeBytes,
	})
	if err != nil {
		return "", err
	}
	return string(result), nil
}

func executeSharedResourceScan(payload SharedResourceScanPayload, dataPath string) (string, error) {
	root, err := cleanSyncPath(payload.RootPath)
	if err != nil {
		return "", err
	}
	target, err := cleanSyncPath(payload.Path)
	if err != nil {
		return "", err
	}
	if !pathWithinRoot(target, root) || !isManagedMountSource(root, dataPath) {
		return "", fmt.Errorf("shared resource scan path escapes managed storage root")
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	resolvedTarget, err := filepath.EvalSymlinks(target)
	if err != nil {
		return "", err
	}
	if !pathWithinRoot(resolvedTarget, resolvedRoot) || !isManagedMountSource(resolvedRoot, dataPath) {
		return "", fmt.Errorf("shared resource scan symlink escapes managed storage root")
	}
	target = resolvedTarget
	items, err := os.ReadDir(target)
	if err != nil {
		return "", err
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].IsDir() != items[j].IsDir() {
			return items[i].IsDir()
		}
		return items[i].Name() < items[j].Name()
	})
	limit := payload.Limit
	if limit < 1 || limit > 1000 {
		limit = 500
	}
	entries := make([]userDirectoryEntry, 0, min(len(items), limit))
	for _, item := range items {
		if len(entries) >= limit {
			break
		}
		info, err := item.Info()
		if err != nil {
			return "", err
		}
		entryType := "file"
		if item.IsDir() {
			entryType = "directory"
		} else if info.Mode()&os.ModeSymlink != 0 {
			entryType = "symlink"
		}
		entries = append(entries, userDirectoryEntry{
			Name: item.Name(), Type: entryType, SizeBytes: info.Size(),
			Mtime: info.ModTime().Unix(), Mode: info.Mode().String(),
		})
	}
	var fileCount int64
	var sizeBytes int64
	err = filepath.WalkDir(target, func(path string, entry os.DirEntry, walkErr error) error {
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
	result, err := json.Marshal(map[string]any{
		"resource_id":   payload.ResourceID,
		"relative_path": payload.RelativePath,
		"path":          target,
		"entries":       entries,
		"truncated":     len(items) > limit,
		"file_count":    fileCount,
		"size_bytes":    sizeBytes,
	})
	if err != nil {
		return "", err
	}
	return string(result), nil
}
