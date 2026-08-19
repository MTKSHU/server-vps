package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
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
	}, "/", nil)
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

func TestExecuteSharedResourceVerifyRejectsEmptyDirectory(t *testing.T) {
	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		ResourceID: 1,
		SourcePath: t.TempDir(),
	})
	if err == nil {
		t.Fatalf("expected empty resource to fail verification, output=%s", output)
	}
}

func TestExecuteSharedResourceVerifyRejectsHFDNeeded(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	hfdDir := filepath.Join(root, ".hfd")
	if err := os.MkdirAll(hfdDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hfdDir, "needed"), []byte("missing.bin\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		ResourceID: 2,
		SourcePath: root,
	})
	if err == nil || !strings.Contains(output, ".hfd/needed is not empty") {
		t.Fatalf("expected hfd needed marker to fail verification, err=%v, output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyRejectsAria2Partial(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("partial"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "file.bin.aria2"), []byte("state"), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		ResourceID: 3,
		SourcePath: root,
	})
	if err == nil || !strings.Contains(output, "partial file remains") {
		t.Fatalf("expected aria2 marker to fail verification, err=%v, output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyAcceptsNonEmptyDirectory(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("complete"), 0o644); err != nil {
		t.Fatal(err)
	}
	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		ResourceID: 4,
		SourcePath: root,
	})
	if err != nil {
		t.Fatalf("expected non-empty resource to pass verification: %v, output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyComparesHFManifest(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/datasets/owner/repo":
			fmt.Fprint(w, `{"sha":"commit-1","siblings":[]}`)
		case "/api/datasets/owner/repo/tree/main":
			fmt.Fprint(w, `[{"type":"file","path":"present.bin","size":4},{"type":"file","path":"missing.bin","size":8}]`)
		default:
			t.Errorf("unexpected endpoint path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "present.bin"), []byte("1234"), 0o644); err != nil {
		t.Fatal(err)
	}
	hfdDir := filepath.Join(root, ".hfd")
	if err := os.MkdirAll(hfdDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hfdDir, "manifest"), []byte("4\tpresent.bin\n8\tmissing.bin\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hfdDir, "repo_metadata.json"), []byte(`{"sha":"commit-1"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		ResourceID: 5,
		SourcePath: root,
		Source:     "huggingface",
		RepoID:     "owner/repo",
		Revision:   "main",
		RepoType:   "dataset",
		HFEndpoint: server.URL,
	})
	if err == nil || !strings.Contains(output, "missing remote file: missing.bin") {
		t.Fatalf("expected missing remote file to fail verification, err=%v output=%s", err, output)
	}
	if !strings.Contains(output, server.URL) {
		t.Fatalf("verification output does not identify the configured endpoint: %s", output)
	}
}

func TestExecuteSharedResourceVerifyRejectsChangedHFRevision(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"sha":"commit-new","siblings":[]}`)
	}))
	defer server.Close()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("1234"), 0o644); err != nil {
		t.Fatal(err)
	}
	hfdDir := filepath.Join(root, ".hfd")
	if err := os.MkdirAll(hfdDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hfdDir, "manifest"), []byte("4\tfile.bin\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hfdDir, "repo_metadata.json"), []byte(`{"sha":"commit-old"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		SourcePath: root,
		Source:     "huggingface",
		RepoID:     "owner/repo",
		Revision:   "main",
		RepoType:   "model",
		HFEndpoint: server.URL,
	})
	if err == nil || !strings.Contains(output, "remote revision changed after download") {
		t.Fatalf("expected changed remote revision to fail verification, err=%v output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyRejectsOfficialRedirect(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://huggingface.co/api/models/owner/repo", http.StatusPermanentRedirect)
	}))
	defer server.Close()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("1234"), 0o644); err != nil {
		t.Fatal(err)
	}
	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		SourcePath: root,
		Source:     "huggingface",
		RepoID:     "owner/repo",
		Revision:   "main",
		RepoType:   "model",
		HFEndpoint: server.URL,
	})
	if err == nil || !strings.Contains(output, "redirected repository API to huggingface.co") {
		t.Fatalf("expected redirect to official endpoint to fail, err=%v output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyFinalizesFromLocalHFMetadata(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://huggingface.co/api/datasets/owner/repo", http.StatusPermanentRedirect)
	}))
	defer server.Close()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("complete"), 0o644); err != nil {
		t.Fatal(err)
	}
	metadataPath := filepath.Join(root, ".cache", "huggingface", "download", "file.bin.metadata")
	if err := os.MkdirAll(filepath.Dir(metadataPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(metadataPath, []byte("commit-1\netag\n123\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		SourcePath:           root,
		Source:               "huggingface",
		RepoID:               "owner/repo",
		Revision:             "main",
		RepoType:             "dataset",
		HFEndpoint:           server.URL,
		AllowOfflineManifest: true,
		ManualFinalize:       true,
	})
	if err != nil {
		t.Fatalf("expected local hfd evidence to permit manual finalization: %v output=%s", err, output)
	}
	if !strings.Contains(output, `"verification_mode":"local_hfd_evidence"`) || !strings.Contains(output, `"downloaded_sha":"commit-1"`) {
		t.Fatalf("expected local verification detail, output=%s", output)
	}
}

func TestExecuteSharedResourceVerifyRejectsIncompleteLocalHFMetadata(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://huggingface.co/api/models/owner/repo", http.StatusPermanentRedirect)
	}))
	defer server.Close()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file.bin"), []byte("complete"), 0o644); err != nil {
		t.Fatal(err)
	}
	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		SourcePath:           root,
		Source:               "huggingface",
		RepoID:               "owner/repo",
		RepoType:             "model",
		HFEndpoint:           server.URL,
		AllowOfflineManifest: true,
	})
	if err == nil || !strings.Contains(output, "local hfd evidence is invalid") {
		t.Fatalf("expected missing local metadata to fail finalization, err=%v output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyDetectsHFHashMismatch(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/models/owner/repo":
			fmt.Fprint(w, `{"sha":"commit-1","siblings":[]}`)
		case "/api/models/owner/repo/tree/main":
			// remote LFS oid does not match the local file content's real sha256.
			fmt.Fprint(w, `[{"type":"file","path":"weights.bin","size":8,"lfs":{"oid":"0000000000000000000000000000000000000000000000000000000000000"}}]`)
		default:
			t.Errorf("unexpected endpoint path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "weights.bin"), []byte("12345678"), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		SourcePath: root,
		Source:     "huggingface",
		RepoID:     "owner/repo",
		Revision:   "main",
		RepoType:   "model",
		HFEndpoint: server.URL,
	})
	if err == nil || !strings.Contains(output, "hash mismatch: weights.bin") {
		t.Fatalf("expected hash mismatch to fail verification, err=%v output=%s", err, output)
	}
}

func TestExecuteSharedResourceVerifyComparesModelScopeManifest(t *testing.T) {
	sha := "1c7a1c8f1a12eb1e40a68cb96a3742871db4048f2c5aefd51a1c8de5aab1f9c5"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"Code":200,"Message":"success","Data":{"Files":[
			{"Type":"blob","Path":"present.bin","Size":4,"Sha256":%q},
			{"Type":"blob","Path":"missing.bin","Size":8,"Sha256":"deadbeef"}
		]}}`, sha)
	}))
	defer server.Close()

	restoreModelScopeAPIBase := modelScopeAPIBase
	modelScopeAPIBase = server.URL
	defer func() { modelScopeAPIBase = restoreModelScopeAPIBase }()

	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "present.bin"), []byte("1234"), 0o644); err != nil {
		t.Fatal(err)
	}

	output, err := executeSharedResourceVerify(SharedResourceVerifyPayload{
		ResourceID: 6,
		SourcePath: root,
		Source:     "modelscope",
		RepoID:     "owner/repo",
		Revision:   "master",
		RepoType:   "model",
	})
	if err == nil || !strings.Contains(output, "missing remote file: missing.bin") {
		t.Fatalf("expected missing remote file to fail verification, err=%v output=%s", err, output)
	}
}
