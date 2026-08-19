package main

import (
	"strings"
	"testing"
)

func TestValidateTrueNASUserNFSShare(t *testing.T) {
	nobody := "nobody"
	root := "root"
	tests := []struct {
		name    string
		share   *trueNASNFSShare
		wantErr string
	}{
		{
			name:  "restricted read-write share",
			share: &trueNASNFSShare{ID: 8, Path: "/mnt/pool/users/alice", Networks: []string{"10.0.0.0/24"}, MapRootUser: &nobody},
		},
		{
			name:    "unrestricted share",
			share:   &trueNASNFSShare{ID: 8, Path: "/mnt/pool/users/alice"},
			wantErr: "no hosts or networks restriction",
		},
		{
			name:    "read-only share",
			share:   &trueNASNFSShare{ID: 8, Path: "/mnt/pool/users/alice", Hosts: []string{"10.0.0.2"}, ReadOnly: true},
			wantErr: "read-only",
		},
		{
			name:    "root mapping",
			share:   &trueNASNFSShare{ID: 8, Path: "/mnt/pool/users/alice", Hosts: []string{"10.0.0.2"}, MapAllUser: &root},
			wantErr: "permits root mapping",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := validateTrueNASUserNFSShare(test.share, "/mnt/pool/users/alice")
			if test.wantErr == "" && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if test.wantErr != "" && (err == nil || !strings.Contains(err.Error(), test.wantErr)) {
				t.Fatalf("expected error containing %q, got %v", test.wantErr, err)
			}
		})
	}
}
