package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const downloaderContainerName = "cluster-resource-downloader"

func waitForDownloaderNetworkRouteScript() string {
	return `
set -eu
for i in $(seq 1 30); do
  ip -4 route show default | grep -q . && exit 0
  sleep 2
done
echo "downloader container did not obtain an IPv4 default route from incusbr0" >&2
exit 1
`
}

func resourceDownloaderNetworkOverrideArgs() []string {
	return []string{"config", "device", "override", downloaderContainerName, "eth0", "parent=incusbr0", "nictype=bridged", "name=eth0"}
}

func ensureResourceDownloaderNetwork() error {
	// Custom Incus default profiles may attach containers to a host bridge that
	// has no DHCP service. Keep a working profile-provided network when it has an
	// IPv4 default route, otherwise fall back to Incus' managed NAT bridge.
	if _, err := runCommandCombinedTimeout(10*time.Second, "incus", "exec", downloaderContainerName, "--", "sh", "-lc", "ip -4 route show default | grep -q ."); err == nil {
		return nil
	}
	if _, err := runCommandCombinedTimeout(10*time.Second, "incus", "network", "show", "incusbr0"); err != nil {
		return fmt.Errorf("downloader container has no IPv4 default route and managed network incusbr0 is unavailable: %w", err)
	}
	// device override copies the inherited nictype property, so override the
	// bridge parent directly; Incus rejects combining nictype with network=.
	if output, err := runCommandCombinedTimeout(30*time.Second, "incus", resourceDownloaderNetworkOverrideArgs()...); err != nil {
		return fmt.Errorf("attach downloader container to incusbr0: %w; %s", err, output)
	}
	if output, err := runCommandCombinedTimeout(70*time.Second, "incus", "exec", downloaderContainerName, "--", "sh", "-lc", waitForDownloaderNetworkRouteScript()); err != nil {
		return fmt.Errorf("wait for downloader container network: %w; %s", err, output)
	}
	return nil
}

func ensureResourceDownloaderContainer(payload DownloadSharedResourcePayload, storagePool string) error {
	if err := os.MkdirAll(payload.StagingPath, 0755); err != nil {
		return fmt.Errorf("mkdir staging path: %w", err)
	}
	if err := os.Chmod(payload.StagingPath, 0777); err != nil {
		return fmt.Errorf("chmod staging path: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(payload.TargetPath), 0755); err != nil {
		return fmt.Errorf("mkdir target parent: %w", err)
	}
	if !incusContainerExists(downloaderContainerName) {
		args := []string{"launch", "images:ubuntu/24.04", downloaderContainerName}
		if strings.TrimSpace(storagePool) != "" {
			args = append(args, "--storage", strings.TrimSpace(storagePool))
		}
		if output, err := runCommandCombinedTimeout(30*time.Minute, "incus", args...); err != nil {
			return fmt.Errorf("launch downloader container: %w; %s", err, output)
		}
		_, _ = runCommandCombined("incus", "config", "set", downloaderContainerName, "user.cluster-role", "resource_downloader")
	}
	if err := ensureContainerRunning(downloaderContainerName, 2*time.Minute); err != nil {
		return err
	}
	if err := ensureResourceDownloaderNetwork(); err != nil {
		return err
	}
	// A previous agent process may have exited while its Incus exec kept running.
	// Wait before changing the container's single staging mount, otherwise two
	// downloads can suddenly operate on the same directory.
	waitForDownloader := `
set -eu
while pgrep -f '[h]fd .*--local-dir /srv/resource-staging' >/dev/null 2>&1; do sleep 5; done
if command -v flock >/dev/null 2>&1; then
  mkdir -p /run/lock
  flock --wait 1209600 /run/lock/cluster-resource-download.lock true
fi
`
	if output, err := runCommandCombinedTimeout(14*24*time.Hour, "incus", "exec", downloaderContainerName, "--", "sh", "-lc", waitForDownloader); err != nil {
		return fmt.Errorf("wait for existing downloader: %w; %s", err, output)
	}
	if err := addOrReplaceDevice(downloaderContainerName, "resource-staging", "disk", "source="+payload.StagingPath, "path=/srv/resource-staging"); err != nil {
		return fmt.Errorf("mount staging into downloader: %w", err)
	}
	source := strings.ToLower(strings.TrimSpace(payload.Source))
	engine := strings.ToLower(strings.TrimSpace(payload.HFDownloadEngine))
	if engine == "" {
		engine = "auto"
	}
	hfdEnabled := "0"
	hfdRequired := "0"
	if source != "modelscope" && engine != "sdk" {
		hfdEnabled = "1"
	}
	if source != "modelscope" && engine == "hfd" {
		hfdRequired = "1"
	}
	bootstrap := fmt.Sprintf(`
set -eu
export DEBIAN_FRONTEND=noninteractive
HFD_ENABLED=%s
HFD_REQUIRED=%s

wait_for_dns() {
  host="$1"
  for i in $(seq 1 60); do
    getent hosts "$host" >/dev/null 2>&1 && return 0
    sleep 5
  done
  return 1
}

ensure_base_tools() {
  if command -v curl >/dev/null 2>&1 && command -v wget >/dev/null 2>&1 && command -v aria2c >/dev/null 2>&1 && command -v jq >/dev/null 2>&1 && command -v bash >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && command -v flock >/dev/null 2>&1; then
    return 0
  fi
  wait_for_dns archive.ubuntu.com || wait_for_dns security.ubuntu.com || {
    echo "Ubuntu package mirror DNS is not ready; cannot install downloader base tools" >&2
    exit 1
  }
  apt-get -o Acquire::Retries=5 update
  apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates curl wget aria2 jq bash python3 python3-venv git util-linux
}

install_official_hfd() {
  [ "$HFD_ENABLED" = "1" ] || return 0
  mkdir -p /opt/resource-downloader/bin
  tmp="/opt/resource-downloader/bin/hfd.sh.tmp"
  for i in $(seq 1 60); do
    if wget -q -O "$tmp" https://hf-mirror.com/hfd/hfd.sh; then
      chmod a+x "$tmp"
      mv "$tmp" /opt/resource-downloader/bin/hfd
      ln -sf /opt/resource-downloader/bin/hfd /usr/local/bin/hfd
      echo "Installed official hfd from https://hf-mirror.com/hfd/hfd.sh"
      return 0
    fi
    rm -f "$tmp"
    sleep 5
  done
  if [ -x /opt/resource-downloader/bin/hfd ]; then
    ln -sf /opt/resource-downloader/bin/hfd /usr/local/bin/hfd
    echo "Could not refresh hfd from hf-mirror; using existing hfd"
    return 0
  fi
  if [ "$HFD_REQUIRED" = "1" ]; then
    echo "hfd is required but https://hf-mirror.com/hfd/hfd.sh is unavailable" >&2
    exit 1
  fi
  echo "hfd is unavailable; auto mode will fall back to Hugging Face SDK"
}

ensure_base_tools
if [ ! -x /opt/resource-downloader/bin/python ]; then
  python3 -m venv /opt/resource-downloader
fi
if ! /opt/resource-downloader/bin/python - <<'PY'
import importlib.util
missing = [m for m in ("huggingface_hub", "modelscope") if importlib.util.find_spec(m) is None]
raise SystemExit(1 if missing else 0)
PY
then
  /opt/resource-downloader/bin/pip install -U pip
  /opt/resource-downloader/bin/pip install -U huggingface_hub modelscope
fi
install_official_hfd
`, hfdEnabled, hfdRequired)
	if output, err := runCommandCombinedTimeout(30*time.Minute, "incus", "exec", downloaderContainerName, "--", "sh", "-lc", bootstrap); err != nil {
		return fmt.Errorf("bootstrap downloader tools: %w; %s", err, output)
	}
	return nil
}

func dirStats(root string) (int64, int64, string) {
	var files int64
	var bytes int64
	latest := ""
	var latestMod time.Time
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return nil
		}
		files++
		bytes += info.Size()
		if info.ModTime().After(latestMod) {
			latestMod = info.ModTime()
			if rel, err := filepath.Rel(root, path); err == nil {
				latest = rel
			} else {
				latest = filepath.Base(path)
			}
		}
		return nil
	})
	return files, bytes, latest
}

func reportDownloadProgress(server string, args cliArgs, hostname string, taskID int, phase string, pct int, stagingPath string, currentFile string) {
	files, bytesDone, latest := dirStats(stagingPath)
	if currentFile == "" {
		currentFile = latest
	}
	if len(currentFile) > 120 {
		currentFile = currentFile[len(currentFile)-120:]
	}
	reportTaskProgress(server, args, hostname, taskID, SyncProgress{
		Phase:       phase,
		Pct:         pct,
		BytesDone:   bytesDone,
		BytesTotal:  0,
		Rate:        fmt.Sprintf("%d files", files),
		CurrentFile: currentFile,
	})
}

func downloadScript(payload DownloadSharedResourcePayload) (string, error) {
	config := map[string]string{
		"resource_id":       fmt.Sprintf("%d", payload.ResourceID),
		"source":            strings.TrimSpace(payload.Source),
		"repo_id":           strings.TrimSpace(payload.RepoID),
		"revision":          strings.TrimSpace(payload.Revision),
		"token":             strings.TrimSpace(payload.Token),
		"repo_type":         strings.TrimSpace(payload.RepoType),
		"hf_endpoint":       strings.TrimSpace(payload.HFEndpoint),
		"hf_engine":         strings.TrimSpace(payload.HFDownloadEngine),
		"fallback_repo_id":  strings.TrimSpace(payload.FallbackRepoID),
		"fallback_revision": strings.TrimSpace(payload.FallbackRevision),
	}
	if config["repo_id"] == "" {
		return "", fmt.Errorf("repo_id is empty")
	}
	if config["revision"] == "" {
		if config["source"] == "modelscope" {
			config["revision"] = "master"
		} else {
			config["revision"] = "main"
		}
	}
	if config["repo_type"] == "" {
		config["repo_type"] = "model"
	}
	data, _ := json.Marshal(config)
	return fmt.Sprintf(`
set -eu
cd /srv/resource-staging
/opt/resource-downloader/bin/python - <<'PY'
import json
import fcntl
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

cfg = json.loads(%s)
local_dir = "/srv/resource-staging"

# A task can be retried after the agent process exits while its previous Incus
# exec is still alive. The container has one dynamic staging mount, so serialize
# every downloader process before touching it.
resource_id = str(cfg.get("resource_id") or "unknown")
os.makedirs("/run/lock", exist_ok=True)
lock_file = open("/run/lock/cluster-resource-download.lock", "w")
fcntl.flock(lock_file, fcntl.LOCK_EX)

def reset_local_dir():
    for name in os.listdir(local_dir):
        path = os.path.join(local_dir, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)

marker_path = os.path.join(local_dir, ".cluster-resource-id")
try:
    with open(marker_path, "r", encoding="utf-8") as marker_file:
        staged_resource_id = marker_file.read().strip()
except FileNotFoundError:
    staged_resource_id = ""
if staged_resource_id and staged_resource_id != resource_id:
    reset_local_dir()
with open(marker_path, "w", encoding="utf-8") as marker_file:
    marker_file.write(resource_id)

source = cfg.get("source") or "huggingface"
repo_type = cfg.get("repo_type") or "model"
revision = cfg.get("revision") or None
token = cfg.get("token") or None
if source == "priority":
    errors = []
    try:
        from modelscope import snapshot_download as ms_snapshot_download
        print(f"[modelscope] downloading {cfg['repo_id']} -> {local_dir}", flush=True)
        ms_snapshot_download(
            cfg["repo_id"],
            repo_type="dataset" if repo_type == "dataset" else "model",
            revision=revision or "master",
            local_dir=local_dir,
            endpoint="https://modelscope.cn",
        )
        print("[ok] downloaded from ModelScope", flush=True)
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as exc:
        errors.append(f"ModelScope: {type(exc).__name__}: {exc}")
        print(f"[warn] ModelScope failed, switching to Hugging Face mirror: {exc}", flush=True)
    try:
        from huggingface_hub import snapshot_download as hf_snapshot_download
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ.pop(key, None)
        endpoint = (cfg.get("hf_endpoint") or "https://hf-mirror.com").rstrip("/")
        os.environ["HF_ENDPOINT"] = endpoint
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        fallback_repo_id = cfg.get("fallback_repo_id") or cfg["repo_id"]
        print(f"[hf-mirror] downloading {fallback_repo_id} -> {local_dir} (endpoint={endpoint})", flush=True)
        hf_snapshot_download(
            repo_id=fallback_repo_id,
            repo_type="dataset" if repo_type == "dataset" else "model",
            revision=cfg.get("fallback_revision") or "main",
            local_dir=local_dir,
            endpoint=endpoint,
        )
        print("[ok] downloaded from Hugging Face mirror", flush=True)
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as exc:
        errors.append(f"Hugging Face mirror: {type(exc).__name__}: {exc}")
        raise RuntimeError("all public download sources failed: " + " | ".join(errors)) from exc
elif source == "modelscope":
    from modelscope.hub.snapshot_download import snapshot_download
    snapshot_download(
        model_id=cfg["repo_id"],
        repo_type="dataset" if repo_type == "dataset" else "model",
        revision=revision,
        token=token,
        local_dir=local_dir,
    )
else:
    from huggingface_hub import snapshot_download
    endpoints = []
    endpoint = (cfg.get("hf_endpoint") or "").strip().rstrip("/")
    if endpoint:
        endpoints.append(endpoint)
    else:
        endpoints.append("")
    last_error = None
    engine = (cfg.get("hf_engine") or "auto").strip().lower()
    if engine not in ("auto", "sdk", "hfd"):
        engine = "auto"
    engines = ["hfd", "sdk"] if engine == "auto" else [engine]

    def reject_official_api_redirect(endpoint_value):
        if not endpoint_value or urllib.parse.urlparse(endpoint_value).hostname == "huggingface.co":
            return
        repo_api_path = ("datasets/" if repo_type == "dataset" else "models/") + cfg["repo_id"]
        if revision and revision != "main":
            repo_api_path += "/revision/" + urllib.parse.quote(revision, safe="")
        request = urllib.request.Request(endpoint_value + "/api/" + repo_api_path + "?blobs=true")
        if token:
            request.add_header("Authorization", "Bearer " + token)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        try:
            response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
            response.close()
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location", "")
            if 300 <= exc.code < 400 and urllib.parse.urlparse(location).hostname == "huggingface.co":
                raise RuntimeError(
                    f"configured HF endpoint redirected repository API to huggingface.co: {location}"
                ) from exc
            raise

    reject_official_api_redirect(endpoint)

    def run_hfd(endpoint_value):
        env = os.environ.copy()
        env["HF_ENDPOINT"] = endpoint_value or "https://huggingface.co"
        cmd = [
            "/opt/resource-downloader/bin/hfd",
            cfg["repo_id"],
            "--local-dir",
            local_dir,
            "--revision",
            revision or "main",
        ]
        if repo_type == "dataset":
            cmd.append("--dataset")
        if token:
            cmd.extend(["--hf_username", "token-user", "--hf_token", token])
        last_hfd_error = None
        for attempt in range(1, 4):
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output_parts = []
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                sys.stdout.write(chunk)
                sys.stdout.flush()
                output_parts.append(chunk)
                if sum(map(len, output_parts)) > 1024 * 1024:
                    output_parts = ["".join(output_parts)[-512 * 1024:]]
            return_code = process.wait()
            output_text = "".join(output_parts)
            completed = "Done." in output_text or "Up to date." in output_text
            try:
                if return_code != 0:
                    raise subprocess.CalledProcessError(return_code, cmd)
                if not completed:
                    raise RuntimeError("hfd exited without a completion marker; download is incomplete")
                return
            except (subprocess.CalledProcessError, RuntimeError) as exc:
                last_hfd_error = exc
                if attempt < 3:
                    print(f"hfd attempt {attempt} failed; retrying to resume file listing/download", flush=True)
        raise last_hfd_error

    def run_sdk(endpoint_value):
        if endpoint_value:
            os.environ["HF_ENDPOINT"] = endpoint_value
        else:
            os.environ.pop("HF_ENDPOINT", None)
        snapshot_download(
            repo_id=cfg["repo_id"],
            repo_type="dataset" if repo_type == "dataset" else "model",
            revision=revision,
            token=token,
            local_dir=local_dir,
            endpoint=endpoint_value or None,
        )

    for endpoint in dict.fromkeys(endpoints):
        label = endpoint or "default"
        for selected_engine in engines:
            print(f"Trying Hugging Face download: engine={selected_engine} endpoint={label}", flush=True)
            try:
                if selected_engine == "hfd":
                    run_hfd(endpoint)
                else:
                    run_sdk(endpoint)
                last_error = None
                raise SystemExit(0)
            except SystemExit:
                raise
            except Exception as exc:
                last_error = exc
                print(f"Download attempt failed: engine={selected_engine} endpoint={label}: {type(exc).__name__}: {exc}", flush=True)
    if last_error is not None:
        raise last_error
PY
`, shellSingleQuote(string(data))), nil
}

func runDownloaderExecWithProgress(payload DownloadSharedResourcePayload, server string, args cliArgs, hostname string, taskID int) (string, error) {
	script, err := downloadScript(payload)
	if err != nil {
		return "", err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 14*24*time.Hour)
	defer cancel()
	cmd := exec.CommandContext(ctx, "incus", "exec", downloaderContainerName, "--", "sh", "-lc", script)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return "", err
	}
	if err := cmd.Start(); err != nil {
		return "", err
	}
	done := make(chan error, 1)
	var output rollingOutput
	var outputMu sync.Mutex
	currentLine := ""
	consume := func(reader io.Reader) {
		scanner := bufio.NewScanner(reader)
		scanner.Buffer(make([]byte, 64*1024), 1024*1024)
		scanner.Split(scanLinesCR)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" {
				continue
			}
			outputMu.Lock()
			output.WriteLine(line)
			currentLine = line
			outputMu.Unlock()
		}
	}
	var readers sync.WaitGroup
	readers.Add(2)
	go func() { defer readers.Done(); consume(stdout) }()
	go func() { defer readers.Done(); consume(stderr) }()
	go func() {
		readers.Wait()
		done <- cmd.Wait()
	}()
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case waitErr := <-done:
			outputMu.Lock()
			text := output.String()
			outputMu.Unlock()
			if ctx.Err() == context.DeadlineExceeded {
				return text, fmt.Errorf("download timed out after 14 days")
			}
			if waitErr != nil {
				return text, fmt.Errorf("download command failed: %s", text)
			}
			return text, nil
		case <-ticker.C:
			outputMu.Lock()
			line := currentLine
			outputMu.Unlock()
			reportDownloadProgress(server, args, hostname, taskID, "downloading", 0, payload.StagingPath, line)
		}
	}
}

const downloaderOutputLimit = 512 * 1024

type rollingOutput struct {
	data      []byte
	truncated bool
}

func (o *rollingOutput) WriteLine(line string) {
	o.data = append(o.data, line...)
	o.data = append(o.data, '\n')
	if len(o.data) > downloaderOutputLimit {
		o.data = append([]byte(nil), o.data[len(o.data)-downloaderOutputLimit:]...)
		o.truncated = true
	}
}

func (o *rollingOutput) String() string {
	text := strings.TrimSpace(string(o.data))
	if o.truncated {
		return "[earlier downloader output omitted]\n" + text
	}
	return text
}

func copyPath(src string, dst string) error {
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	if info.IsDir() {
		return filepath.WalkDir(src, func(path string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			rel, err := filepath.Rel(src, path)
			if err != nil {
				return err
			}
			target := filepath.Join(dst, rel)
			if entry.IsDir() {
				info, err := entry.Info()
				if err != nil {
					return err
				}
				return os.MkdirAll(target, info.Mode().Perm())
			}
			return copyFile(path, target)
		})
	}
	return copyFile(src, dst)
}

func copyFile(src string, dst string) error {
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode().Perm())
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return err
	}
	return out.Close()
}

func activateDownloadedResource(staging string, target string) error {
	if err := os.RemoveAll(target); err != nil {
		return fmt.Errorf("remove old target: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		return fmt.Errorf("mkdir target parent: %w", err)
	}
	if err := os.Rename(staging, target); err != nil {
		if linkErr, ok := err.(*os.LinkError); !ok || linkErr.Err != syscall.EXDEV {
			return fmt.Errorf("activate downloaded resource: %w", err)
		}
		if err := copyPath(staging, target); err != nil {
			return fmt.Errorf("copy downloaded resource across filesystems: %w", err)
		}
		if err := os.RemoveAll(staging); err != nil {
			return fmt.Errorf("remove staging after copy: %w", err)
		}
	}
	return nil
}

func executeDownloadSharedResource(payload DownloadSharedResourcePayload, storagePool string, server string, args cliArgs, hostname string, taskID int) (string, error) {
	if payload.TargetPath == "" || payload.StagingPath == "" {
		return "", fmt.Errorf("target_path and staging_path are required")
	}
	target, err := cleanSyncPath(payload.TargetPath)
	if err != nil {
		return "", err
	}
	staging, err := cleanSyncPath(payload.StagingPath)
	if err != nil {
		return "", err
	}
	payload.TargetPath = target
	payload.StagingPath = staging
	if err := seedStagingFromTarget(staging, target); err != nil {
		return "", err
	}
	reportDownloadProgress(server, args, hostname, taskID, "preparing", 0, staging, "")
	prepareDone := make(chan struct{})
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				reportDownloadProgress(server, args, hostname, taskID, "preparing", 0, staging, "")
			case <-prepareDone:
				return
			}
		}
	}()
	prepareErr := ensureResourceDownloaderContainer(payload, storagePool)
	close(prepareDone)
	if prepareErr != nil {
		return "", prepareErr
	}
	output, err := runDownloaderExecWithProgress(payload, server, args, hostname, taskID)
	if err != nil {
		return output, err
	}
	reportDownloadProgress(server, args, hostname, taskID, "finalizing", 95, staging, "")
	if err := activateDownloadedResource(staging, target); err != nil {
		return output, err
	}
	if err := os.MkdirAll(staging, 0755); err != nil {
		return output, fmt.Errorf("recreate staging path: %w", err)
	}
	_ = os.Chmod(staging, 0777)
	_ = addOrReplaceDevice(downloaderContainerName, "resource-staging", "disk", "source="+staging, "path=/srv/resource-staging")
	reportDownloadProgress(server, args, hostname, taskID, "done", 100, target, "")
	return strings.TrimSpace(output), nil
}

func executePrepareSharedResourceDownload(payload DownloadSharedResourcePayload, storagePool string) (string, error) {
	if payload.TargetPath == "" {
		return "", fmt.Errorf("target_path is required")
	}
	target, err := cleanSyncPath(payload.TargetPath)
	if err != nil {
		return "", err
	}
	payload.TargetPath = target
	payload.StagingPath = target
	if err := ensureResourceDownloaderContainer(payload, storagePool); err != nil {
		return "", err
	}
	if err := os.WriteFile(filepath.Join(target, ".cluster-resource-id"), []byte(strconv.Itoa(payload.ResourceID)+"\n"), 0644); err != nil {
		return "", fmt.Errorf("write staging resource identity: %w", err)
	}
	result, _ := json.Marshal(map[string]any{
		"resource_id":    payload.ResourceID,
		"target_path":    target,
		"container_name": downloaderContainerName,
		"container_path": "/srv/resource-staging",
	})
	return string(result), nil
}

// seedStagingFromTarget recovers data from a prior false-success or an
// interrupted refresh. The paths are siblings in normal deployments, so the
// rename is atomic and hfd can continue from files already on disk.
func seedStagingFromTarget(staging string, target string) error {
	if !pathExists(target) {
		return nil
	}
	if pathExists(staging) {
		entries, err := os.ReadDir(staging)
		if err != nil {
			return fmt.Errorf("read staging path: %w", err)
		}
		if len(entries) > 0 {
			return nil
		}
		if err := os.Remove(staging); err != nil {
			return fmt.Errorf("remove empty staging path: %w", err)
		}
	}
	if err := os.Rename(target, staging); err != nil {
		return fmt.Errorf("seed staging from existing target: %w", err)
	}
	return nil
}
