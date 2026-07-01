package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExecuteDataSyncAppliesBandwidthLimit(t *testing.T) {
	binDir := t.TempDir()
	rsyncPath := filepath.Join(binDir, "rsync")
	if err := os.WriteFile(rsyncPath, []byte("#!/bin/sh\nprintf '%s\\n' \"$@\"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	source := t.TempDir()
	target := t.TempDir()
	output, err := executeDataSync(DataSyncPayload{
		SourcePath:     source,
		TargetPath:     target,
		BandwidthLimit: 7,
	}, "/")
	if err != nil {
		t.Fatal(err)
	}
	// BandwidthLimit=7 Mbps → 7*125=875 KB/s
	if !strings.Contains(output, "--bwlimit=875") {
		t.Fatalf("bandwidth argument missing from rsync invocation: %s", output)
	}
}

func TestValidateRestorePayload(t *testing.T) {
	dataRoot := t.TempDir()
	sourceRoot := filepath.Join(dataRoot, "backups", "users", "alice", "100")
	targetRoot := filepath.Join(dataRoot, "restores", "users", "alice", "200")
	if err := os.MkdirAll(sourceRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	payload := DataSyncPayload{
		Mode: "restore_user_home", Username: "alice",
		SourceRoot: sourceRoot, SourcePath: sourceRoot,
		TargetRoot: targetRoot, TargetPath: targetRoot,
	}
	if err := validateRestorePayload(payload, dataRoot); err != nil {
		t.Fatalf("valid restore rejected: %v", err)
	}
}

func TestMakeRestrictedRsyncPath(t *testing.T) {
	allowed := "/mnt/data/cluster-storage/datasets/IFMTBench/Tencent-Hunyuan"
	rel, err := makeRestrictedRsyncPath(allowed, allowed)
	if err != nil || rel != "." {
		t.Fatalf("expected '.', got %q, err=%v", rel, err)
	}
	rel, err = makeRestrictedRsyncPath(allowed, allowed+"/subdir")
	if err != nil || rel != "subdir" {
		t.Fatalf("expected 'subdir', got %q, err=%v", rel, err)
	}
	_, err = makeRestrictedRsyncPath(allowed, "/other/path")
	if err == nil {
		t.Fatal("expected error for path outside allowed root")
	}
}

func TestRemoteRsyncSourceRestrictedPath(t *testing.T) {
	endpoint := DataSyncSSHEndpoint{
		Host:        "storage.example.com",
		Port:        2222,
		User:        "root",
		Restricted:  true,
		AllowedPath: "/mnt/data/cluster-storage/datasets/IFMTBench/Tencent-Hunyuan",
	}
	remote, _, err := remoteRsyncSource(endpoint, "/mnt/data/cluster-storage/datasets/IFMTBench/Tencent-Hunyuan")
	if err != nil {
		t.Fatal(err)
	}
	expected := "root@storage.example.com:./"
	if remote != expected {
		t.Fatalf("expected %q, got %q", expected, remote)
	}
}

func TestRemoteRsyncTargetRestrictedPath(t *testing.T) {
	endpoint := DataSyncSSHEndpoint{
		Host:        "storage.example.com",
		Port:        2222,
		User:        "root",
		Restricted:  true,
		AllowedPath: "/mnt/data/cluster-storage/datasets/IFMTBench/Tencent-Hunyuan",
	}
	remote, _, err := remoteRsyncTarget(endpoint, "/mnt/data/cluster-storage/datasets/IFMTBench/Tencent-Hunyuan")
	if err != nil {
		t.Fatal(err)
	}
	expected := "root@storage.example.com:./"
	if remote != expected {
		t.Fatalf("expected %q, got %q", expected, remote)
	}
}

func TestValidateRestorePayloadRejectsTargetSymlinkEscape(t *testing.T) {
	dataRoot := t.TempDir()
	sourceRoot := filepath.Join(dataRoot, "backups", "users", "alice", "100")
	homeRoot := filepath.Join(dataRoot, "users", "alice")
	if err := os.MkdirAll(sourceRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(homeRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	escaped := filepath.Join(homeRoot, "escaped")
	if err := os.Symlink(t.TempDir(), escaped); err != nil {
		t.Fatal(err)
	}
	err := validateRestorePayload(DataSyncPayload{
		Mode: "restore_user_home", Username: "alice",
		SourceRoot: sourceRoot, SourcePath: sourceRoot,
		TargetRoot: homeRoot, TargetPath: escaped,
	}, dataRoot)
	if err == nil {
		t.Fatal("expected restore target symlink escape to be rejected")
	}
}
