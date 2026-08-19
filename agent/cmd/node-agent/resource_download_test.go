package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDownloadScriptPreservesResourceCheckpointAndValidatesHFD(t *testing.T) {
	script, err := downloadScript(DownloadSharedResourcePayload{
		ResourceID:       293,
		Source:           "huggingface",
		RepoID:           "Insta360-Research/OmniRooms",
		RepoType:         "dataset",
		HFEndpoint:       "https://hf-mirror.com",
		HFDownloadEngine: "auto",
	})
	if err != nil {
		t.Fatal(err)
	}

	for _, expected := range []string{
		`cluster-resource-download.lock`,
		`.cluster-resource-id`,
		`hfd exited without a completion marker`,
		`stderr=subprocess.STDOUT`,
	} {
		if !strings.Contains(script, expected) {
			t.Fatalf("download script does not contain %q", expected)
		}
	}
	if strings.Contains(script, "clean_local_dir()") {
		t.Fatal("download script must not clear checkpoints between retries")
	}
	if !strings.Contains(script, "if endpoint:\n        endpoints.append(endpoint)\n    else:\n        endpoints.append(\"\")") {
		t.Fatal("custom HF endpoint must be strict and must not append the official endpoint fallback")
	}
	if !strings.Contains(script, "endpoint=endpoint_value or None") {
		t.Fatal("Hugging Face SDK fallback must receive the endpoint explicitly")
	}
	if !strings.Contains(script, "configured HF endpoint redirected repository API to huggingface.co") {
		t.Fatal("custom HF endpoint must reject repository API redirects to huggingface.co")
	}
}

func TestDownloadScriptPublicPriorityFallback(t *testing.T) {
	script, err := downloadScript(DownloadSharedResourcePayload{
		ResourceID: 9, Source: "priority", RepoID: "owner/ms-repo", Revision: "master",
		FallbackRepoID: "owner/hf-repo", FallbackRevision: "main",
		RepoType: "dataset", HFEndpoint: "https://hf-mirror.com",
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{
		`source == "priority"`,
		`ModelScope failed, switching to Hugging Face mirror`,
		`HF_HUB_DISABLE_XET`,
		`("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")`,
		`cfg.get("fallback_repo_id")`,
	} {
		if !strings.Contains(script, expected) {
			t.Fatalf("priority downloader script missing %q", expected)
		}
	}
}

func TestDownloaderNetworkFallbackWaitsForIPv4DefaultRoute(t *testing.T) {
	if !strings.Contains(waitForDownloaderNetworkRouteScript(), "ip -4 route show default") {
		t.Fatal("downloader network fallback must wait for an IPv4 default route")
	}
	args := strings.Join(resourceDownloaderNetworkOverrideArgs(), " ")
	if !strings.Contains(args, "parent=incusbr0") || strings.Contains(args, "network=incusbr0") {
		t.Fatalf("downloader network override uses incompatible Incus device properties: %s", args)
	}
}

func TestRollingOutputKeepsTailWithinLimit(t *testing.T) {
	var output rollingOutput
	output.WriteLine(strings.Repeat("a", downloaderOutputLimit))
	output.WriteLine("final error")

	text := output.String()
	if !strings.HasPrefix(text, "[earlier downloader output omitted]\n") {
		t.Fatal("expected truncation marker")
	}
	if !strings.HasSuffix(text, "final error") {
		t.Fatal("expected latest output to be retained")
	}
}

func TestSeedStagingFromTargetRecoversExistingDownload(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "resource")
	staging := filepath.Join(root, ".partial")
	if err := os.MkdirAll(target, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(target, "downloaded.bin"), []byte("partial"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(staging, 0755); err != nil {
		t.Fatal(err)
	}

	if err := seedStagingFromTarget(staging, target); err != nil {
		t.Fatal(err)
	}
	if pathExists(target) {
		t.Fatal("target should have moved back to staging")
	}
	if !pathExists(filepath.Join(staging, "downloaded.bin")) {
		t.Fatal("existing downloaded file was not preserved")
	}
}

func TestSeedStagingFromTargetKeepsNonEmptyCheckpoint(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "resource")
	staging := filepath.Join(root, ".partial")
	for _, path := range []string{target, staging} {
		if err := os.MkdirAll(path, 0755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(staging, "checkpoint"), []byte("keep"), 0644); err != nil {
		t.Fatal(err)
	}

	if err := seedStagingFromTarget(staging, target); err != nil {
		t.Fatal(err)
	}
	if !pathExists(target) || !pathExists(filepath.Join(staging, "checkpoint")) {
		t.Fatal("non-empty staging and current target should remain unchanged")
	}
}
