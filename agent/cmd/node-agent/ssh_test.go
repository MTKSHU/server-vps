package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestIsContainerHomePath(t *testing.T) {
	if !isContainerHomePath("/home/alice", "alice") {
		t.Fatal("expected /home/alice to be detected as alice's home")
	}
	if isContainerHomePath("/home/bob", "alice") {
		t.Fatal("expected another user's home path to be allowed")
	}
}

func TestAuthorizedKeysDeduplicates(t *testing.T) {
	keys := authorizedKeys("ssh-ed25519 AAAAalice alice@laptop", "ssh-ed25519 AAAAalice alice@laptop")
	if keys != "ssh-ed25519 AAAAalice alice@laptop" {
		t.Fatalf("unexpected authorized_keys content: %q", keys)
	}
}

func TestHomeSkeletonSeedCopiesOnlyMissingEntries(t *testing.T) {
	root := t.TempDir()
	skel := filepath.Join(root, "skel")
	home := filepath.Join(root, "home")
	if err := os.MkdirAll(filepath.Join(skel, ".config"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(home, 0o755); err != nil {
		t.Fatal(err)
	}
	for path, content := range map[string]string{
		filepath.Join(skel, ".bashrc"):            "default bashrc\n",
		filepath.Join(skel, ".profile"):           "default profile\n",
		filepath.Join(skel, ".config", "example"): "default config\n",
		filepath.Join(home, ".bashrc"):            "custom bashrc\n",
	} {
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	command := exec.Command("sh", "-c", homeSkeletonSeedScript(), "sh", home, skel)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("seed home: %v: %s", err, output)
	}

	for path, want := range map[string]string{
		filepath.Join(home, ".bashrc"):            "custom bashrc\n",
		filepath.Join(home, ".profile"):           "default profile\n",
		filepath.Join(home, ".config", "example"): "default config\n",
	} {
		got, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read %s: %v", path, err)
		}
		if string(got) != want {
			t.Fatalf("unexpected content for %s: got %q want %q", path, got, want)
		}
	}
}
