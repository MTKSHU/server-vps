package main

import (
	"errors"
	"os"
	"strings"
	"testing"
)

func TestNormalizedNFSOptionsAddsHard(t *testing.T) {
	value, err := normalizedNFSOptions("_netdev,noatime,vers=4.1,proto=tcp")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(value, "hard") || strings.Contains(value, "_netdev") {
		t.Fatalf("unexpected mount options: %q", value)
	}
}

func TestNormalizedNFSOptionsRejectsSoft(t *testing.T) {
	if _, err := normalizedNFSOptions("vers=4.1,soft"); err == nil {
		t.Fatal("soft NFS mount must be rejected")
	}
}

func TestManagedNFSMountDoesNotCreateMissingSource(t *testing.T) {
	err := validateManagedMount(ManagedMount{
		Kind: "user_home", Source: managedNFSRoot + "/users/definitely-missing-user",
		Target: "/home/ubuntu", Required: true,
	})
	if err == nil {
		t.Fatal("missing required NFS source must fail closed")
	}
}

func TestPerUserNFSExportMustUseDedicatedSingleDirectory(t *testing.T) {
	valid := ManagedMount{
		Kind: "user_home", Source: managedNFSRoot + "/user-datasets/admin",
		Target: "/home/ubuntu", Export: "/mnt/pool/users/admin", Required: false,
	}
	if err := validateManagedMount(valid); err != nil {
		t.Fatalf("valid per-user mount rejected: %v", err)
	}
	invalid := valid
	invalid.Source = managedNFSRoot + "/user-datasets/admin/nested"
	if err := validateManagedMount(invalid); err == nil {
		t.Fatal("nested per-user mount source must be rejected")
	}
}

func TestManagedNFSUnusedOnlyForMissingState(t *testing.T) {
	if !managedNFSUnused(os.ErrNotExist) {
		t.Fatal("missing state must mean managed NFS is unused")
	}
	if managedNFSUnused(nil) || managedNFSUnused(errors.New("invalid state")) {
		t.Fatal("valid or broken state must not be classified as unused")
	}
}
