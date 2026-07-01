package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

func executeTask(task *AgentTask, args cliArgs) TaskResultRequest {
	switch task.Type {
	case "incus_create_container":
		var payload IncusCreatePayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		storagePool := args.incusStoragePool
		if storagePool == "" {
			storagePool = detectIncusStoragePool()
		}
		if storagePool == "" {
			return TaskResultRequest{OK: false, Status: "failed", Error: "Incus storage pool 未配置，且无法自动探测"}
		}
		ip, err := executeIncusCreate(payload, storagePool, args.dataPath)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "running", IP: ip}
	case "incus_exec_command":
		var payload IncusExecPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := runCommandCombined("incus", "exec", payload.Name, "--", "sh", "-lc", payload.Command)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "incus_config_update":
		var payload IncusConfigUpdatePayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		storagePool := args.incusStoragePool
		if storagePool == "" {
			storagePool = detectIncusStoragePool()
		}
		if err := executeIncusConfigUpdate(payload, storagePool); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded"}
	case "incus_sync_ssh_keys":
		var payload IncusSSHKeysPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		if err := syncContainerSSHKeys(payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded"}
	case "incus_publish_container":
		var payload IncusPublishPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := runCommandCombinedTimeout(60*time.Minute, "incus", "publish", payload.Name, "--alias", payload.Alias, "--force")
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		if strings.TrimSpace(payload.ExportDir) != "" {
			exportOutput, exportErr := executeIncusImageExport(IncusImageExportPayload{
				ImageRef:  payload.Alias,
				Alias:     payload.Alias,
				ExportDir: payload.ExportDir,
				BaseName:  payload.BaseName,
			})
			output = strings.TrimSpace(output + "\n" + exportOutput)
			if exportErr != nil {
				return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: exportErr.Error()}
			}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "incus_sync_ports":
		var payload IncusPortsPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		if err := syncIncusPorts(payload.Name, payload.Ports); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		if hasSSHPort(payload.Ports) {
			if err := initializeContainerSSHWithMounts(payload.Name, payload.SSHUsername, payload.Mounts, payload.SSHKey); err != nil {
				return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
			}
		}
		return TaskResultRequest{OK: true, Status: "succeeded"}
	case "incus_start_container", "incus_stop_container", "incus_restart_container", "incus_delete_container":
		var payload IncusLifecyclePayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		ip, err := executeIncusLifecycle(payload)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", IP: ip}
	case "node_shutdown":
		if err := scheduleNodePower("shutdown"); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: "shutdown scheduled"}
	case "node_reboot":
		if err := scheduleNodePower("reboot"); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: "reboot scheduled"}
	case "trigger_agent_update":
		output, err := runCommandCombined("systemctl", "start", "cluster-agent-updater")
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: "updater started"}
	case "incus_image_pull":
		var payload IncusImagePullPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		imageCopyArgs := []string{"image", "copy", "--auto-update", payload.ImageRef, "local:"}
		if payload.Alias != "" {
			imageCopyArgs = append(imageCopyArgs, "--alias", payload.Alias)
		}
		output, err := runCommandCombinedTimeout(60*time.Minute, "incus", imageCopyArgs...)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "ssh_pubkey_install":
		var payload SshPubkeyInstallPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		if err := installSshPubkey(payload.Pubkey); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded"}
	case "incus_image_export":
		var payload IncusImageExportPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeIncusImageExport(payload)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "incus_image_import":
		var payload IncusImageImportPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeIncusImageImport(payload, args.dataPath)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "incus_image_cleanup":
		var payload IncusImageCleanupPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeIncusImageCleanup(payload)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "container_data_sync":
		var payload DataSyncPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeContainerDataSync(payload, args.dataPath)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "install_sync_pubkey":
		var payload InstallSyncPubkeyPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		if err := installSyncPubkey(payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded"}
	case "remove_sync_pubkey":
		var payload RemoveSyncPubkeyPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		if err := removeSyncPubkey(payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded"}
	case "verify_shared_resource":
		var payload SharedResourceVerifyPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeSharedResourceVerify(payload)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "scan_user_directory":
		var payload UserDirectoryScanPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeUserDirectoryScan(payload, args.dataPath)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "ensure_user_zfs_dataset":
		var payload UserZFSDatasetPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeEnsureUserZFSDataset(payload)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "scan_shared_resource":
		var payload SharedResourceScanPayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeSharedResourceScan(payload, args.dataPath)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "remove_user_zfs_dataset":
		var payload UserZFSDatasetRemovePayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeRemoveUserZFSDataset(payload)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	case "remove_user_workspace_volume":
		var payload UserWorkspaceVolumeRemovePayload
		if err := json.Unmarshal(task.Payload, &payload); err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Error: err.Error()}
		}
		output, err := executeRemoveUserWorkspaceVolume(payload, args.incusStoragePool)
		if err != nil {
			return TaskResultRequest{OK: false, Status: "failed", Output: output, Error: err.Error()}
		}
		return TaskResultRequest{OK: true, Status: "succeeded", Output: output}
	default:
		return TaskResultRequest{OK: false, Status: "failed", Error: "unknown task type: " + task.Type}
	}
}

func installSshPubkey(pubkey string) error {
	home := os.Getenv("HOME")
	if home == "" {
		home = "/root"
	}
	sshDir := home + "/.ssh"
	if err := os.MkdirAll(sshDir, 0700); err != nil {
		return fmt.Errorf("mkdir ~/.ssh: %w", err)
	}
	authKeysPath := sshDir + "/authorized_keys"
	existing, _ := os.ReadFile(authKeysPath)
	key := strings.TrimSpace(pubkey)
	if strings.Contains(string(existing), key) {
		return nil
	}
	f, err := os.OpenFile(authKeysPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		return fmt.Errorf("open authorized_keys: %w", err)
	}
	defer f.Close()
	_, err = fmt.Fprintf(f, "\n%s\n", key)
	return err
}

// slowTaskSem 限制并发慢任务数量（最多 1 个），避免同时跑多个耗时 I/O 操作打满节点资源。
var slowTaskSem = make(chan struct{}, 1)

// slowTaskTypes 是执行时间可能超过数十秒的任务类型集合。
// 这些任务将在独立 goroutine 中运行，让任务轮询循环可立即继续处理其他任务。
var slowTaskTypes = map[string]bool{
	"container_data_sync":     true, // rsync 跨节点数据同步，通常 1–10+ 分钟
	"incus_image_pull":        true, // 从远端拉取镜像，可能数十分钟
	"incus_image_export":      true, // 将镜像导出到磁盘，分钟级
	"incus_image_import":      true, // 从磁盘导入镜像，分钟级
	"incus_publish_container": true, // 将容器压缩为镜像（+ 可选导出），分钟级
	"scan_user_directory":     true, // du/find 扫描用户目录，大数据集时分钟级
	"scan_shared_resource":    true, // 共享资源扫描，分钟级
	"verify_shared_resource":  true, // 共享资源校验，分钟级
	"incus_exec_command":      true, // 任意容器内命令，最长 10 分钟超时
}

func runTask(server string, args cliArgs, hostname string, task *AgentTask) {
	fmt.Printf("%s claimed task %d type=%s attempt=%d\n", time.Now().Format(time.RFC3339), task.ID, task.Type, task.Attempts)
	result := executeTask(task, args)
	if err := reportTask(server, args, hostname, task.ID, result); err != nil {
		fmt.Fprintf(os.Stderr, "%s report task %d failed: %v\n", time.Now().Format(time.RFC3339), task.ID, err)
		return
	}
	if result.OK {
		fmt.Printf("%s completed task %d status=%s ip=%s\n", time.Now().Format(time.RFC3339), task.ID, result.Status, result.IP)
	} else {
		fmt.Fprintf(os.Stderr, "%s failed task %d: %s\n", time.Now().Format(time.RFC3339), task.ID, result.Error)
	}
}

func processTasks(server string, args cliArgs, hostname string) {
	for {
		task, err := claimTask(server, args, hostname)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s claim task failed: %v\n", time.Now().Format(time.RFC3339), err)
			return
		}
		if task == nil {
			return
		}
		if slowTaskTypes[task.Type] {
			select {
			case slowTaskSem <- struct{}{}:
				// 获取慢任务槽位，在独立 goroutine 中执行，让循环立即继续轮询下一个任务
				go func(t *AgentTask) {
					defer func() {
						if r := recover(); r != nil {
							fmt.Fprintf(os.Stderr, "%s panic in slow task %d type=%s: %v\n",
								time.Now().Format(time.RFC3339), t.ID, t.Type, r)
						}
						<-slowTaskSem
					}()
					runTask(server, args, hostname, t)
				}(task)
			default:
				// 慢任务槽位已满（已有慢任务正在运行），同步执行（任务已 claim，不可退还）
				runTask(server, args, hostname, task)
			}
		} else {
			// 快速任务（秒级），同步执行
			runTask(server, args, hostname, task)
		}
	}
}
