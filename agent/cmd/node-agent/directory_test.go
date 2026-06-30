package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestExecuteUserDirectoryScan(t *testing.T) {
	dataRoot := t.TempDir()
	home := filepath.Join(dataRoot, "users", "alice")
	if err := os.MkdirAll(filepath.Join(home, "work"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, "note.txt"), []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("/etc", filepath.Join(home, "outside")); err != nil {
		t.Fatal(err)
	}
	output, err := executeUserDirectoryScan(UserDirectoryScanPayload{
		UserID: 1, Username: "alice", RootPath: home, Path: home, Limit: 100,
	}, dataRoot)
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Entries   []userDirectoryEntry `json:"entries"`
		FileCount int64                `json:"file_count"`
		SizeBytes int64                `json:"size_bytes"`
	}
	if err := json.Unmarshal([]byte(output), &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Entries) != 3 || result.FileCount != 2 || result.SizeBytes < 5 {
		t.Fatalf("unexpected directory scan: %+v", result)
	}
}

func TestExecuteUserDirectoryScanRejectsEscape(t *testing.T) {
	dataRoot := t.TempDir()
	home := filepath.Join(dataRoot, "users", "alice")
	if err := os.MkdirAll(home, 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := executeUserDirectoryScan(UserDirectoryScanPayload{
		Username: "alice", RootPath: home, Path: filepath.Join(home, "..", "bob"), Limit: 100,
	}, dataRoot)
	if err == nil {
		t.Fatal("expected escaped directory path to be rejected")
	}
}

func TestExecuteUserDirectoryScanRejectsSymlinkDirectoryEscape(t *testing.T) {
	dataRoot := t.TempDir()
	home := filepath.Join(dataRoot, "users", "alice")
	outside := t.TempDir()
	if err := os.MkdirAll(home, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(home, "outside-dir")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	_, err := executeUserDirectoryScan(UserDirectoryScanPayload{
		Username: "alice", RootPath: home, Path: link, Limit: 100,
	}, dataRoot)
	if err == nil {
		t.Fatal("expected symlink directory escape to be rejected")
	}
}
