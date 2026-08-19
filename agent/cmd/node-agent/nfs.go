package main

import (
	"bufio"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const managedNFSRoot = "/var/lib/server-vps/nfs"
const managedNFSStatePath = managedNFSRoot + "/.mount-state.json"

var safeSentinel = regexp.MustCompile(`^\.?[A-Za-z0-9][A-Za-z0-9._-]{1,80}$`)
var safeSentinelSignature = regexp.MustCompile(`^[A-Za-z0-9_-]{16,128}$`)

type managedNFSState struct {
	Config  SharedStorageConfig `json:"config"`
	Needed  []string            `json:"needed"`
	Exports []managedNFSExport  `json:"exports,omitempty"`
}

type managedNFSExport struct {
	Export    string `json:"export"`
	Target    string `json:"target"`
	UserOwned bool   `json:"user_owned,omitempty"`
}

func persistManagedNFSState(config SharedStorageConfig, needed map[string]bool, directExports []managedNFSExport) error {
	if len(needed) == 0 && len(directExports) == 0 {
		return nil
	}
	if err := os.MkdirAll(managedNFSRoot, 0o755); err != nil {
		return err
	}
	hasDirectUserExports := false
	for _, item := range directExports {
		if item.UserOwned {
			hasDirectUserExports = true
			break
		}
	}
	if existing, err := loadManagedNFSState(); err == nil {
		for _, name := range existing.Needed {
			if name == "users" || name == "datasets" || name == "models" {
				if name == "users" && hasDirectUserExports {
					continue
				}
				needed[name] = true
			}
		}
		directExports = append(directExports, existing.Exports...)
	}
	names := []string{}
	for _, name := range []string{"users", "datasets", "models"} {
		if name == "users" && hasDirectUserExports {
			continue
		}
		if needed[name] {
			names = append(names, name)
		}
	}
	uniqueExports := make([]managedNFSExport, 0, len(directExports))
	seenTargets := map[string]bool{}
	seenExports := map[string]bool{}
	for _, item := range directExports {
		item.Export = filepath.Clean(strings.TrimSpace(item.Export))
		item.Target = filepath.Clean(strings.TrimSpace(item.Target))
		if item.Export == "." || item.Target == "." || seenTargets[item.Target] || seenExports[item.Export] {
			continue
		}
		seenTargets[item.Target] = true
		seenExports[item.Export] = true
		uniqueExports = append(uniqueExports, item)
	}
	data, err := json.Marshal(managedNFSState{Config: config, Needed: names, Exports: uniqueExports})
	if err != nil {
		return err
	}
	temporary := managedNFSStatePath + ".tmp"
	if err := os.WriteFile(temporary, data, 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, managedNFSStatePath)
}

func loadManagedNFSState() (managedNFSState, error) {
	var state managedNFSState
	data, err := os.ReadFile(managedNFSStatePath)
	if err != nil {
		return state, err
	}
	err = json.Unmarshal(data, &state)
	return state, err
}

func managedNFSUnused(stateErr error) bool {
	return errors.Is(stateErr, os.ErrNotExist)
}

func validateIDMapRange(base int) error {
	if base < 65536 {
		return fmt.Errorf("invalid idmap base %d", base)
	}
	for _, path := range []string{"/etc/subuid", "/etc/subgid"} {
		file, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}
		covered := false
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			parts := strings.Split(strings.TrimSpace(scanner.Text()), ":")
			if len(parts) != 3 {
				continue
			}
			start, startErr := strconv.Atoi(parts[1])
			count, countErr := strconv.Atoi(parts[2])
			if startErr == nil && countErr == nil && start <= base && start+count >= base+65536 {
				covered = true
				break
			}
		}
		closeErr := file.Close()
		if scanner.Err() != nil {
			return fmt.Errorf("scan %s: %w", path, scanner.Err())
		}
		if closeErr != nil {
			return fmt.Errorf("close %s: %w", path, closeErr)
		}
		if !covered {
			return fmt.Errorf("%s does not cover idmap range %d-%d", path, base, base+65535)
		}
	}
	return nil
}

func validateManagedMount(mount ManagedMount) error {
	source := filepath.Clean(strings.TrimSpace(mount.Source))
	target := filepath.Clean(strings.TrimSpace(mount.Target))
	if source == "." || target == "." || !filepath.IsAbs(source) || !filepath.IsAbs(target) || target == "/" {
		return fmt.Errorf("invalid managed mount %q -> %q", mount.Source, mount.Target)
	}
	if strings.HasPrefix(source, managedNFSRoot+string(os.PathSeparator)) {
		if mount.Export != "" {
			exportPath := filepath.Clean(strings.TrimSpace(mount.Export))
			userHomesRoot := filepath.Join(managedNFSRoot, "user-datasets")
			relativeSource, relativeErr := filepath.Rel(userHomesRoot, source)
			if !filepath.IsAbs(exportPath) || relativeErr != nil || relativeSource == "." || relativeSource == ".." || strings.Contains(relativeSource, string(os.PathSeparator)) {
				return fmt.Errorf("invalid per-user NFS mount %q at %q", mount.Export, mount.Source)
			}
		}
		if _, err := os.Stat(source); err != nil {
			if mount.Required {
				return fmt.Errorf("required NFS source %s is unavailable: %w", source, err)
			}
		}
		return nil
	}
	if mount.Kind != "node_cache" && mount.Kind != "scratch" && mount.Kind != "legacy" {
		return fmt.Errorf("managed mount kind %s cannot use non-NFS source %s", mount.Kind, source)
	}
	return ensureMountSource(source, "/")
}

func readSentinel(target, sentinel string, userOwned bool, idMapBase int) ([]byte, error) {
	path := filepath.Join(target, sentinel)
	data, directErr := os.ReadFile(path)
	if directErr == nil || !userOwned {
		return data, directErr
	}
	uid := strconv.Itoa(idMapBase + 1000)
	output, err := runCommandCombinedTimeout(5*time.Second, "setpriv", "--reuid="+uid, "--regid="+uid, "--clear-groups", "cat", "--", path)
	if err != nil {
		return nil, fmt.Errorf("read directly: %v; read as mapped user %s: %w (%s)", directErr, uid, err, strings.TrimSpace(output))
	}
	return []byte(output), nil
}

func normalizedNFSOptions(raw string) (string, error) {
	options := []string{}
	hasHard := false
	hasVersion := false
	hasProtocol := false
	for _, item := range strings.Split(raw, ",") {
		item = strings.TrimSpace(item)
		if item == "" || item == "_netdev" {
			continue
		}
		if strings.HasPrefix(strings.ToLower(item), "soft") {
			return "", fmt.Errorf("soft NFS mounts are prohibited")
		}
		if strings.HasPrefix(item, "vers=") || strings.HasPrefix(item, "nfsvers=") {
			if !strings.HasSuffix(item, "=4.1") {
				return "", fmt.Errorf("NFS version must be 4.1")
			}
			hasVersion = true
		}
		if strings.HasPrefix(item, "proto=") {
			if item != "proto=tcp" {
				return "", fmt.Errorf("NFS transport must be TCP")
			}
			hasProtocol = true
		}
		if item == "hard" {
			hasHard = true
		}
		options = append(options, item)
	}
	if !hasHard {
		options = append(options, "hard")
	}
	if !hasVersion {
		options = append(options, "vers=4.1")
	}
	if !hasProtocol {
		options = append(options, "proto=tcp")
	}
	return strings.Join(options, ","), nil
}

func ensureNFSExport(server, exportPath, target, options, sentinel, sentinelSignature string, userOwned bool, idMapBase int) error {
	return ensureNFSExportWithRetry(server, exportPath, target, options, sentinel, sentinelSignature, userOwned, idMapBase, true)
}

func ensureNFSExportWithRetry(server, exportPath, target, options, sentinel, sentinelSignature string, userOwned bool, idMapBase int, allowRemount bool) error {
	server, exportPath = strings.TrimSpace(server), strings.TrimSpace(exportPath)
	if server == "" || strings.ContainsAny(server, " /\t\r\n") || !filepath.IsAbs(exportPath) {
		return fmt.Errorf("invalid NFS endpoint %q:%q", server, exportPath)
	}
	if !safeSentinel.MatchString(sentinel) {
		return fmt.Errorf("invalid NFS sentinel")
	}
	if !safeSentinelSignature.MatchString(sentinelSignature) {
		return fmt.Errorf("invalid NFS sentinel signature")
	}
	if err := os.MkdirAll(target, 0o755); err != nil {
		return fmt.Errorf("prepare NFS mountpoint: %w", err)
	}
	expected := server + ":" + exportPath
	// -M checks that target itself is a mountpoint. --target would also return
	// the containing root filesystem for an unmounted directory and prevent the
	// NFS mount from ever being attempted.
	info := strings.Fields(runCommand("findmnt", "-n", "-o", "FSTYPE,SOURCE", "--mountpoint", target))
	if len(info) == 0 {
		mountOptions, err := normalizedNFSOptions(options)
		if err != nil {
			return err
		}
		if output, err := runCommandCombinedTimeout(30*time.Second, "mount", "-t", "nfs4", "-o", mountOptions, expected, target); err != nil {
			return fmt.Errorf("mount NFS %s: %w (%s)", expected, err, strings.TrimSpace(output))
		}
		info = strings.Fields(runCommand("findmnt", "-n", "-o", "FSTYPE,SOURCE", "--mountpoint", target))
	}
	if len(info) < 2 || !strings.HasPrefix(info[0], "nfs") || info[1] != expected {
		return fmt.Errorf("NFS mount source mismatch at %s: got %q, expected %q", target, strings.Join(info, " "), expected)
	}
	sentinelData, err := readSentinel(target, sentinel, userOwned, idMapBase)
	if err != nil {
		if userOwned && allowRemount {
			// A bind mount held by an existing container namespace can make a
			// normal umount busy. Lazy detach preserves that namespace's old
			// file handle while allowing this host path to be mounted correctly
			// for subsequent containers.
			if output, unmountErr := runCommandCombinedTimeout(10*time.Second, "umount", "-l", target); unmountErr == nil {
				return ensureNFSExportWithRetry(server, exportPath, target, options, sentinel, sentinelSignature, userOwned, idMapBase, false)
			} else {
				return fmt.Errorf("NFS sentinel missing and stale mount could not be refreshed at %s: %w (%s)", target, unmountErr, strings.TrimSpace(output))
			}
		}
		return fmt.Errorf("NFS sentinel missing at %s: %w", target, err)
	}
	actualSignature := strings.TrimSpace(string(sentinelData))
	if subtle.ConstantTimeCompare([]byte(actualSignature), []byte(sentinelSignature)) != 1 {
		if userOwned && allowRemount {
			if output, unmountErr := runCommandCombinedTimeout(10*time.Second, "umount", "-l", target); unmountErr == nil {
				return ensureNFSExportWithRetry(server, exportPath, target, options, sentinel, sentinelSignature, userOwned, idMapBase, false)
			} else {
				return fmt.Errorf("NFS sentinel mismatch and stale mount could not be refreshed at %s: %w (%s)", target, unmountErr, strings.TrimSpace(output))
			}
		}
		return fmt.Errorf("NFS sentinel signature mismatch at %s", target)
	}
	return nil
}

func ensureSharedStorage(config SharedStorageConfig, mounts []ManagedMount) error {
	if !config.Enabled || len(mounts) == 0 {
		return nil
	}
	if err := validateIDMapRange(config.IDMapBase); err != nil {
		return err
	}
	needed := map[string]bool{}
	directExports := []managedNFSExport{}
	for _, mount := range mounts {
		clean := filepath.Clean(mount.Source)
		if strings.TrimSpace(mount.Export) != "" {
			directExports = append(directExports, managedNFSExport{Export: mount.Export, Target: clean, UserOwned: mount.Kind == "user_home"})
			continue
		}
		for _, name := range []string{"users", "datasets", "models"} {
			root := filepath.Join(managedNFSRoot, name)
			if clean == root || strings.HasPrefix(clean, root+string(os.PathSeparator)) {
				needed[name] = true
			}
		}
	}
	exports := map[string]string{"users": config.UsersExport, "datasets": config.DatasetsExport, "models": config.ModelsExport}
	if err := persistManagedNFSState(config, needed, directExports); err != nil {
		return fmt.Errorf("persist managed NFS configuration: %w", err)
	}
	for _, name := range []string{"users", "datasets", "models"} {
		if needed[name] {
			if err := ensureNFSExport(config.Server, exports[name], filepath.Join(managedNFSRoot, name), config.MountOptions, config.Sentinel, config.SentinelSignature, false, config.IDMapBase); err != nil {
				return err
			}
		}
	}
	for _, item := range directExports {
		if err := ensureNFSExport(config.Server, item.Export, item.Target, config.MountOptions, config.Sentinel, config.SentinelSignature, item.UserOwned, config.IDMapBase); err != nil {
			return err
		}
	}
	for _, mount := range mounts {
		if err := validateManagedMount(mount); err != nil {
			return err
		}
	}
	return nil
}

func validateActiveManagedMounts(mounts []ManagedMount) error {
	for _, mount := range mounts {
		if !strings.HasPrefix(filepath.Clean(mount.Source), managedNFSRoot+string(os.PathSeparator)) {
			continue
		}
		if strings.TrimSpace(mount.Export) != "" {
			info := strings.Fields(runCommand("findmnt", "-n", "-o", "FSTYPE,SOURCE", "--mountpoint", filepath.Clean(mount.Source)))
			expected := strings.TrimSpace(mount.Export)
			if len(info) < 2 || !strings.HasPrefix(info[0], "nfs") || !strings.HasSuffix(info[1], ":"+expected) {
				return fmt.Errorf("required per-user NFS mount %s is not active", mount.Source)
			}
			continue
		}
		rootParts := strings.Split(strings.TrimPrefix(filepath.Clean(mount.Source), managedNFSRoot+string(os.PathSeparator)), string(os.PathSeparator))
		if len(rootParts) == 0 {
			return fmt.Errorf("invalid managed NFS path %s", mount.Source)
		}
		root := filepath.Join(managedNFSRoot, rootParts[0])
		info := strings.Fields(runCommand("findmnt", "-n", "-o", "FSTYPE", "--mountpoint", root))
		if len(info) == 0 || !strings.HasPrefix(info[0], "nfs") {
			return fmt.Errorf("required NFS mount %s is not active", root)
		}
		if _, err := os.Stat(mount.Source); err != nil {
			return fmt.Errorf("required NFS source %s is unavailable: %w", mount.Source, err)
		}
	}
	return nil
}

func detectNFSHealth() NFSHealthReport {
	report := NFSHealthReport{CheckedAt: time.Now().Unix()}
	start := time.Now()
	state, stateErr := loadManagedNFSState()
	if stateErr != nil && !managedNFSUnused(stateErr) {
		report.Error = fmt.Sprintf("load managed NFS state: %v", stateErr)
		return report
	}
	if stateErr == nil {
		if err := validateIDMapRange(state.Config.IDMapBase); err != nil {
			report.Error = err.Error()
			return report
		}
		exports := map[string]string{"users": state.Config.UsersExport, "datasets": state.Config.DatasetsExport, "models": state.Config.ModelsExport}
		for _, name := range state.Needed {
			if name != "users" && name != "datasets" && name != "models" {
				report.Error = "invalid persisted NFS mount name"
				return report
			}
			if err := ensureNFSExport(state.Config.Server, exports[name], filepath.Join(managedNFSRoot, name),
				state.Config.MountOptions, state.Config.Sentinel, state.Config.SentinelSignature, false, state.Config.IDMapBase); err != nil {
				report.Error = err.Error()
				return report
			}
		}
		for _, item := range state.Exports {
			if err := ensureNFSExport(state.Config.Server, item.Export, item.Target,
				state.Config.MountOptions, state.Config.Sentinel, state.Config.SentinelSignature, item.UserOwned, state.Config.IDMapBase); err != nil {
				report.Error = err.Error()
				return report
			}
		}
	}
	found := false
	ignoreLegacyUsers := false
	if stateErr == nil && len(state.Exports) > 0 {
		ignoreLegacyUsers = true
		for _, name := range state.Needed {
			if name == "users" {
				ignoreLegacyUsers = false
				break
			}
		}
	}
	for _, name := range []string{"users", "datasets", "models"} {
		if name == "users" && ignoreLegacyUsers {
			continue
		}
		root := filepath.Join(managedNFSRoot, name)
		info := strings.Fields(runCommandTimeout(3*time.Second, "findmnt", "-n", "-o", "FSTYPE", "--mountpoint", root))
		if len(info) == 0 {
			continue
		}
		found = true
		if !strings.HasPrefix(info[0], "nfs") {
			report.Error = fmt.Sprintf("%s is mounted as %s, not NFS", root, info[0])
			return report
		}
		if _, err := os.Stat(root); err != nil {
			report.Error = err.Error()
			return report
		}
	}
	if stateErr == nil {
		for _, item := range state.Exports {
			info := strings.Fields(runCommandTimeout(3*time.Second, "findmnt", "-n", "-o", "FSTYPE", "--mountpoint", item.Target))
			if len(info) == 0 {
				continue
			}
			found = true
			if !strings.HasPrefix(info[0], "nfs") {
				report.Error = fmt.Sprintf("%s is mounted as %s, not NFS", item.Target, info[0])
				return report
			}
		}
	}
	report.LatencyMS = float64(time.Since(start).Microseconds()) / 1000
	// A missing state file means this node has never needed managed NFS. It is
	// idle, not unhealthy. A requested mount persists state before mounting, so
	// actual mount failures are returned above with their concrete error.
	report.Healthy = found || managedNFSUnused(stateErr)
	if !report.Healthy {
		report.Error = "managed NFS is not mounted"
	}
	return report
}
