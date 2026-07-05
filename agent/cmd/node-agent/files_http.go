package main

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// defaultAgentFilesPort 是 agent 文件 API 的默认监听端口。
// 可通过启动参数 --files-port 覆盖；设为 0 时禁用。
const defaultAgentFilesPort = 8082

type fileLsEntry struct {
	Name      string `json:"name"`
	Type      string `json:"type"`
	SizeBytes int64  `json:"size_bytes"`
	Mtime     int64  `json:"mtime"`
	Mode      string `json:"mode"`
}

type fileLsResponse struct {
	Status    string        `json:"status"`
	Entries   []fileLsEntry `json:"entries"`
	FileCount int           `json:"file_count"`
	SizeBytes int64         `json:"size_bytes"`
	Truncated bool          `json:"truncated"`
	Error     string        `json:"error,omitempty"`
}

// startAgentFilesServer 在指定端口启动轻量 HTTP 文件列表服务。
// 仅暴露 GET /api/files/ls，使用 Bearer token 鉴权。
// 后端通过该接口替代 SSH ls，实现毫秒级目录列表。
func startAgentFilesServer(token, dataPath string, port int) {
	mux := http.NewServeMux()

	mux.HandleFunc("/api/files/ls", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// 鉴权
		authHeader := r.Header.Get("Authorization")
		expected := "Bearer " + token
		if subtle.ConstantTimeCompare([]byte(authHeader), []byte(expected)) != 1 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprint(w, `{"error":"unauthorized"}`)
			return
		}

		q := r.URL.Query()
		rawPath := q.Get("path")
		rawRoot := q.Get("root")
		zfsDataset := q.Get("zfs_dataset")
		limit := 500
		if n, err := strconv.Atoi(q.Get("limit")); err == nil && n > 0 && n <= 1000 {
			limit = n
		}

		// 路径合法性检查
		absPath, err := cleanSyncPath(rawPath)
		if err != nil {
			writeFilesJSON(w, http.StatusBadRequest, fileLsResponse{Error: "invalid path: " + err.Error()})
			return
		}
		absRoot, err := cleanSyncPath(rawRoot)
		if err != nil {
			writeFilesJSON(w, http.StatusBadRequest, fileLsResponse{Error: "invalid root: " + err.Error()})
			return
		}
		if !pathWithinRoot(absPath, absRoot) || !isManagedMountSource(absRoot, dataPath) {
			writeFilesJSON(w, http.StatusForbidden, fileLsResponse{Error: "path escapes allowed root"})
			return
		}

		// 符号链接解析后再检查（防止 symlink escape）
		resolvedRoot, err := filepath.EvalSymlinks(absRoot)
		if err != nil {
			writeFilesJSON(w, http.StatusNotFound, fileLsResponse{Error: "root directory not found"})
			return
		}
		resolvedPath, err := filepath.EvalSymlinks(absPath)
		if err != nil {
			// 目录不存在 → 返回 empty-ready，与 SSH 行为一致
			writeFilesJSON(w, http.StatusOK, fileLsResponse{
				Status:  "ready",
				Entries: []fileLsEntry{},
				Error:   "directory not found: " + absPath,
			})
			return
		}
		if !pathWithinRoot(resolvedPath, resolvedRoot) || !isManagedMountSource(resolvedRoot, dataPath) {
			writeFilesJSON(w, http.StatusForbidden, fileLsResponse{Error: "symlink escapes allowed root"})
			return
		}

		items, err := os.ReadDir(resolvedPath)
		if err != nil {
			writeFilesJSON(w, http.StatusInternalServerError, fileLsResponse{Error: "readdir: " + err.Error()})
			return
		}

		// 目录在前，按名称升序排列
		sort.Slice(items, func(i, j int) bool {
			if items[i].IsDir() != items[j].IsDir() {
				return items[i].IsDir()
			}
			return items[i].Name() < items[j].Name()
		})

		entries := make([]fileLsEntry, 0, min(len(items), limit))
		truncated := len(items) > limit
		var fileCount int
		var totalSize int64

		for _, item := range items {
			if len(entries) >= limit {
				break
			}
			info, err := item.Info()
			if err != nil {
				continue
			}
			entryType := "file"
			if item.IsDir() {
				entryType = "directory"
			} else if info.Mode()&os.ModeSymlink != 0 {
				entryType = "symlink"
			}
			if entryType != "directory" {
				fileCount++
				totalSize += info.Size()
			}
			entries = append(entries, fileLsEntry{
				Name:      item.Name(),
				Type:      entryType,
				SizeBytes: info.Size(),
				Mtime:     info.ModTime().Unix(),
				Mode:      info.Mode().String(),
			})
		}

		// 根目录优先使用 ZFS 数据集统计真实用量
		if zfsDataset != "" {
			if used, err := getZFSUsedBytes(zfsDataset); err == nil {
				totalSize = used
			}
		}

		writeFilesJSON(w, http.StatusOK, fileLsResponse{
			Status:    "ready",
			Entries:   entries,
			FileCount: fileCount,
			SizeBytes: totalSize,
			Truncated: truncated,
		})
	})

	// 健康检查端点，供后端探活
	mux.HandleFunc("/api/files/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
	})

	addr := fmt.Sprintf(":%d", port)
	go func() {
		if err := http.ListenAndServe(addr, mux); err != nil {
			fmt.Fprintf(os.Stderr, "[agent-files] HTTP server error: %v\n", err)
		}
	}()
	fmt.Printf("[agent-files] Files API listening on %s\n", addr)
}

// getZFSUsedBytes 通过 `zfs get -Hp used` 获取数据集实际占用字节数。
// -H 去掉表头，-p 返回原始字节数（不带单位），更易解析。
func getZFSUsedBytes(dataset string) (int64, error) {
	out, err := exec.Command("zfs", "get", "-Hp", "-o", "value", "used", dataset).Output()
	if err != nil {
		return 0, err
	}
	return strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
}

func writeFilesJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		fmt.Fprintf(os.Stderr, "[agent-files] JSON encode error: %v\n", err)
	}
}
