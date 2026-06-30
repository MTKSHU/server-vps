package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type config struct {
	server, token, hostname, binary, service string
}

type manifest struct {
	UpdateAvailable bool   `json:"update_available"`
	CurrentVersion  string `json:"current_version"`
	Version         string `json:"version"`
	SHA256          string `json:"sha256"`
	DownloadURL     string `json:"download_url"`
	DeferredReason  string `json:"deferred_reason"`
}

var client = &http.Client{Timeout: 10 * time.Minute}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" { return value }
	return fallback
}

func main() {
	cfg := config{}
	flag.StringVar(&cfg.server, "server", env("CLUSTER_SERVER_URL", ""), "central backend URL")
	flag.StringVar(&cfg.token, "token", env("CLUSTER_NODE_TOKEN", ""), "node token")
	flag.StringVar(&cfg.hostname, "hostname", env("CLUSTER_HOSTNAME", ""), "node hostname")
	flag.StringVar(&cfg.binary, "binary", env("CLUSTER_AGENT_BINARY", "/usr/local/bin/cluster-node-agent"), "agent binary path")
	flag.StringVar(&cfg.service, "service", env("CLUSTER_AGENT_SERVICE", "cluster-node-agent.service"), "systemd service")
	flag.Parse()
	if cfg.hostname == "" { cfg.hostname, _ = os.Hostname() }
	if cfg.server == "" || cfg.token == "" { fatal("--server and --token are required") }
	if err := run(cfg); err != nil { fatal(err.Error()) }
}

func fatal(message string) {
	fmt.Fprintln(os.Stderr, message)
	os.Exit(1)
}

func request(cfg config, method, path string, body io.Reader) (*http.Response, error) {
	req, err := http.NewRequest(method, strings.TrimRight(cfg.server, "/")+path, body)
	if err != nil { return nil, err }
	req.Header.Set("Authorization", "Bearer "+cfg.token)
	req.Header.Set("X-Agent-Hostname", cfg.hostname)
	if body != nil { req.Header.Set("Content-Type", "application/json") }
	return client.Do(req)
}

func run(cfg config) error {
	resp, err := request(cfg, http.MethodGet, "/api/agent-updates/manifest?architecture="+runtime.GOARCH, nil)
	if err != nil { return err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK { return responseError(resp) }
	var target manifest
	if err := json.NewDecoder(resp.Body).Decode(&target); err != nil { return err }
	if !target.UpdateAvailable {
		if target.DeferredReason != "" {
			fmt.Printf("agent update deferred: %s\n", target.DeferredReason)
			return nil
		}
		fmt.Printf("agent is current (%s)\n", target.CurrentVersion)
		return nil
	}
	report(cfg, "downloading", "")
	newPath := cfg.binary + ".new"
	backupPath := cfg.binary + ".bak"
	if err := download(cfg, target, newPath); err != nil {
		report(cfg, "failed", err.Error())
		return err
	}
	defer os.Remove(newPath)
	if output, err := exec.Command(newPath, "--version").CombinedOutput(); err != nil || strings.TrimSpace(string(output)) != target.Version {
		err := fmt.Errorf("downloaded binary version mismatch: got %q, want %q", strings.TrimSpace(string(output)), target.Version)
		report(cfg, "failed", err.Error())
		return err
	}
	report(cfg, "installing", "")
	os.Remove(backupPath)
	if err := os.Rename(cfg.binary, backupPath); err != nil { return fail(cfg, fmt.Errorf("backup current agent: %w", err)) }
	if err := os.Rename(newPath, cfg.binary); err != nil {
		os.Rename(backupPath, cfg.binary)
		return fail(cfg, fmt.Errorf("install new agent: %w", err))
	}
	if err := restartAndCheck(cfg); err != nil {
		os.Remove(cfg.binary)
		rollbackErr := os.Rename(backupPath, cfg.binary)
		if rollbackErr == nil { rollbackErr = restartAndCheck(cfg) }
		message := err.Error()
		if rollbackErr != nil { message += "; rollback failed: " + rollbackErr.Error() }
		report(cfg, "rolled_back", message)
		return errors.New(message)
	}
	report(cfg, "updated", "")
	fmt.Printf("updated agent from %s to %s\n", target.CurrentVersion, target.Version)
	return nil
}

func download(cfg config, target manifest, destination string) error {
	resp, err := request(cfg, http.MethodGet, target.DownloadURL, nil)
	if err != nil { return err }
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK { return responseError(resp) }
	if err := os.MkdirAll(filepath.Dir(destination), 0755); err != nil { return err }
	file, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0755)
	if err != nil { return err }
	hash := sha256.New()
	_, copyErr := io.Copy(io.MultiWriter(file, hash), resp.Body)
	closeErr := file.Close()
	if copyErr != nil { return copyErr }
	if closeErr != nil { return closeErr }
	actual := hex.EncodeToString(hash.Sum(nil))
	if actual != strings.ToLower(target.SHA256) { return fmt.Errorf("sha256 mismatch: got %s, want %s", actual, target.SHA256) }
	return nil
}

func restartAndCheck(cfg config) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if output, err := exec.CommandContext(ctx, "systemctl", "restart", cfg.service).CombinedOutput(); err != nil {
		return fmt.Errorf("restart agent: %w: %s", err, strings.TrimSpace(string(output)))
	}
	time.Sleep(3 * time.Second)
	if output, err := exec.CommandContext(ctx, "systemctl", "is-active", "--quiet", cfg.service).CombinedOutput(); err != nil {
		return fmt.Errorf("agent did not become active: %w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}

func report(cfg config, status, message string) {
	body, _ := json.Marshal(map[string]string{"status": status, "error": message})
	resp, err := request(cfg, http.MethodPost, "/api/agent-updates/report", bytes.NewReader(body))
	if err == nil { io.Copy(io.Discard, resp.Body); resp.Body.Close() }
}

func fail(cfg config, err error) error { report(cfg, "failed", err.Error()); return err }

func responseError(resp *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	return fmt.Errorf("server returned %s: %s", resp.Status, strings.TrimSpace(string(body)))
}
