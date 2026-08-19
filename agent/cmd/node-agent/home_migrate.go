package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func executeContainerHomeMigration(payload ContainerHomeMigrationPayload) (string, error) {
	if payload.Name == "" || payload.SSHUsername == "" || payload.Owner == "" {
		return "", fmt.Errorf("invalid home migration payload")
	}
	if incusContainerStatus(payload.Name) != "stopped" {
		return "", fmt.Errorf("container %s must be stopped before home migration", payload.Name)
	}
	if err := ensureSharedStorage(payload.SharedStorage, []ManagedMount{payload.ManagedMount}); err != nil {
		return "", err
	}
	tempRoot, err := os.MkdirTemp("", "server-vps-home-migrate-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(tempRoot)
	sourceRef := fmt.Sprintf("%s/home/%s", payload.Name, payload.SSHUsername)
	pullOutput, err := runCommandCombined("incus", "file", "pull", "--recursive", sourceRef, tempRoot)
	if err != nil {
		return pullOutput, fmt.Errorf("pull local container home: %w", err)
	}
	pulled := filepath.Join(tempRoot, payload.SSHUsername)
	if _, err := os.Stat(pulled); err != nil {
		pulled = tempRoot
	}
	ownerID := payload.SharedStorage.IDMapBase + 1000
	if ownerID < 65536 {
		return pullOutput, fmt.Errorf("invalid shared storage idmap base")
	}
	// The NFS export uses root_squash. Run all destination writes as the mapped
	// container user, and make the temporary pull readable by that identity.
	if output, err := runCommandCombined("chown", "-R", fmt.Sprintf("%d:%d", ownerID, ownerID), tempRoot); err != nil {
		return strings.TrimSpace(pullOutput + "\n" + output), fmt.Errorf("prepare pulled home ownership: %w", err)
	}
	credentialArgs := []string{"--reuid", strconv.Itoa(ownerID), "--regid", strconv.Itoa(ownerID), "--clear-groups"}
	destination := payload.ManagedMount.Source
	if !payload.Primary {
		destination = filepath.Join(destination, ".migration", payload.Name, "home")
		mkdirArgs := append(append([]string{}, credentialArgs...), "mkdir", "-p", destination)
		if output, err := runCommandCombined("setpriv", mkdirArgs...); err != nil {
			return strings.TrimSpace(pullOutput + "\n" + output), fmt.Errorf("create conflict directory: %w", err)
		}
	}
	rsyncArgs := append(append([]string{}, credentialArgs...), "rsync", "-rlpt", "--ignore-existing",
		strings.TrimRight(pulled, "/")+"/", strings.TrimRight(destination, "/")+"/")
	rsyncOutput, err := runCommandCombined("setpriv", rsyncArgs...)
	if err != nil {
		return strings.TrimSpace(pullOutput + "\n" + rsyncOutput), fmt.Errorf("seed NFS home: %w", err)
	}
	if output, err := runCommandCombined("incus", "config", "set", payload.Name, "security.idmap.base", strconv.Itoa(payload.SharedStorage.IDMapBase)); err != nil {
		return strings.TrimSpace(pullOutput + "\n" + rsyncOutput + "\n" + output), fmt.Errorf("set container idmap base: %w", err)
	}
	deviceArgs := []string{"disk", "source=" + payload.ManagedMount.Source, "path=" + payload.ManagedMount.Target, "required=true"}
	if err := addOrReplaceDevice(payload.Name, safeDeviceName("managed", 0, payload.ManagedMount.Target), deviceArgs...); err != nil {
		return strings.TrimSpace(pullOutput + "\n" + rsyncOutput), fmt.Errorf("attach migrated home: %w", err)
	}
	return strings.TrimSpace(pullOutput + "\n" + rsyncOutput), nil
}
