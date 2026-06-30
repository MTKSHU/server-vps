package main

import (
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

func TestIsDeprecatedHomeCachePath(t *testing.T) {
	if !isDeprecatedHomeCachePath("/home/alice/.cache/huggingface", "alice") {
		t.Fatal("expected Hugging Face cache mount to be deprecated")
	}
	if !isDeprecatedHomeCachePath("/home/alice/.cache/torch/checkpoints", "alice") {
		t.Fatal("expected nested PyTorch cache mount to be deprecated")
	}
	if isDeprecatedHomeCachePath("/home/alice/work", "alice") {
		t.Fatal("expected unrelated home subdirectory to be allowed")
	}
}

func TestAuthorizedKeysDeduplicates(t *testing.T) {
	keys := authorizedKeys("ssh-ed25519 AAAAalice alice@laptop", "ssh-ed25519 AAAAalice alice@laptop")
	if keys != "ssh-ed25519 AAAAalice alice@laptop" {
		t.Fatalf("unexpected authorized_keys content: %q", keys)
	}
}
