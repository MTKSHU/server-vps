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

func TestAuthorizedKeysDeduplicates(t *testing.T) {
	keys := authorizedKeys("ssh-ed25519 AAAAalice alice@laptop", "ssh-ed25519 AAAAalice alice@laptop")
	if keys != "ssh-ed25519 AAAAalice alice@laptop" {
		t.Fatalf("unexpected authorized_keys content: %q", keys)
	}
}
