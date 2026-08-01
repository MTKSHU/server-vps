package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type cliArgs struct {
	server            string
	token             string
	interval          int
	hostname          string
	ip                string
	nodeGroup         string
	driverPool        string
	dataPath          string
	incusStoragePool  string
	taskPollInterval  int
	metricsInterval   int
	containerInterval int
	storageInterval   int
	inventoryInterval int
	filesPort         int
	filesToken        string
}

func main() {
	version := flag.Bool("version", false, "print version and exit")
	var args cliArgs
	flag.StringVar(&args.server, "server", "", "central backend URL, for example http://10.10.0.2:8080")
	flag.StringVar(&args.token, "token", "", "shared registration token")
	flag.IntVar(&args.interval, "interval", 0, "heartbeat interval in seconds; 0 reports once")
	flag.StringVar(&args.hostname, "hostname", "", "override hostname")
	flag.StringVar(&args.ip, "ip", "", "override IP")
	flag.StringVar(&args.nodeGroup, "node-group", "unassigned", "node group label")
	flag.StringVar(&args.driverPool, "driver-pool", "", "override driver pool")
	flag.StringVar(&args.dataPath, "data-path", "/", "managed storage root for host directory mounts")
	flag.StringVar(&args.incusStoragePool, "incus-storage-pool", "", "Incus storage pool for disk capacity reporting and new containers")
	flag.IntVar(&args.taskPollInterval, "task-poll-interval", 5, "task polling interval in seconds between heartbeats")
	flag.IntVar(&args.metricsInterval, "metrics-interval", 2, "CPU, memory and GPU metrics interval in seconds")
	flag.IntVar(&args.containerInterval, "container-interval", 15, "container status refresh interval in seconds")
	flag.IntVar(&args.storageInterval, "storage-interval", 60, "storage capacity refresh interval in seconds")
	flag.IntVar(&args.inventoryInterval, "inventory-interval", 300, "static inventory refresh interval in seconds")
	flag.IntVar(&args.filesPort, "files-port", defaultAgentFilesPort, "port for the files HTTP API (0 to disable)")
	flag.StringVar(&args.filesToken, "files-token", "", "Bearer token for the files HTTP API (defaults to the node token)")
	flag.Parse()
	if *version {
		fmt.Println(agentVersion)
		return
	}

	if args.server == "" {
		args.server = os.Getenv("CLUSTER_SERVER_URL")
	}
	if args.token == "" {
		args.token = os.Getenv("CLUSTER_NODE_TOKEN")
	}
	if args.hostname == "" {
		args.hostname = os.Getenv("CLUSTER_HOSTNAME")
	}
	if args.ip == "" {
		args.ip = os.Getenv("CLUSTER_NODE_IP")
	}
	if args.nodeGroup == "unassigned" && os.Getenv("CLUSTER_NODE_GROUP") != "" {
		args.nodeGroup = os.Getenv("CLUSTER_NODE_GROUP")
	}
	if args.driverPool == "" {
		args.driverPool = os.Getenv("CLUSTER_DRIVER_POOL")
	}
	if args.dataPath == "/" && os.Getenv("CLUSTER_DATA_PATH") != "" {
		args.dataPath = os.Getenv("CLUSTER_DATA_PATH")
	}
	if args.incusStoragePool == "" {
		args.incusStoragePool = os.Getenv("CLUSTER_INCUS_STORAGE_POOL")
	}
	if os.Getenv("CLUSTER_TASK_POLL_INTERVAL") != "" {
		if value, err := strconv.Atoi(os.Getenv("CLUSTER_TASK_POLL_INTERVAL")); err == nil {
			args.taskPollInterval = value
		}
	}
	if args.taskPollInterval < 1 {
		args.taskPollInterval = 1
	}
	if value, err := strconv.Atoi(os.Getenv("CLUSTER_METRICS_INTERVAL")); err == nil && value > 0 {
		args.metricsInterval = value
	}
	if value, err := strconv.Atoi(os.Getenv("CLUSTER_CONTAINER_INTERVAL")); err == nil && value > 0 {
		args.containerInterval = value
	}
	if value, err := strconv.Atoi(os.Getenv("CLUSTER_STORAGE_INTERVAL")); err == nil && value > 0 {
		args.storageInterval = value
	}
	if value, err := strconv.Atoi(os.Getenv("CLUSTER_INVENTORY_INTERVAL")); err == nil && value > 0 {
		args.inventoryInterval = value
	}
	if args.storageInterval < 1 {
		args.storageInterval = 60
	}
	if args.inventoryInterval < 1 {
		args.inventoryInterval = 300
	}
	if os.Getenv("CLUSTER_AGENT_FILES_PORT") != "" {
		if value, err := strconv.Atoi(os.Getenv("CLUSTER_AGENT_FILES_PORT")); err == nil {
			args.filesPort = value
		}
	}
	if args.filesToken == "" {
		args.filesToken = os.Getenv("CLUSTER_AGENT_FILES_TOKEN")
	}
	if args.filesToken == "" {
		// Backward compatibility for nodes where the backend files API token was
		// configured to the same value as this node's registration token.
		args.filesToken = args.token
	}

	if args.server == "" || args.token == "" {
		fmt.Fprintln(os.Stderr, "--server and --token are required")
		os.Exit(2)
	}
	endpoint := strings.TrimRight(args.server, "/") + "/api/nodes/register"
	var terminalOnce sync.Once
	heartbeatCount := 0
	const heartbeatLogEvery = 10 // 每 10 次心跳才打印一次成功日志
	collector := newPayloadCollector(args)
	fallbackConfig := normalizeCollectionConfig(AgentCollectionConfig{
		MetricsIntervalSeconds:   args.metricsInterval,
		HeartbeatIntervalSeconds: args.interval,
		ContainerIntervalSeconds: args.containerInterval,
		StorageIntervalSeconds:   args.storageInterval,
		InventoryIntervalSeconds: args.inventoryInterval,
		TaskPollIntervalSeconds:  args.taskPollInterval,
	}, AgentCollectionConfig{
		MetricsIntervalSeconds:   2,
		HeartbeatIntervalSeconds: 15,
		ContainerIntervalSeconds: 15,
		StorageIntervalSeconds:   60,
		InventoryIntervalSeconds: 300,
		TaskPollIntervalSeconds:  5,
	})
	runtimeConfig := newRuntimeConfigStore(fallbackConfig)
	collector.applyConfig(fallbackConfig)

	// sendHeartbeat 发送一次心跳，返回 (hostname, 是否成功)。
	// 只在心跳 goroutine 内调用，无并发竞争。
	sendHeartbeat := func() (string, bool) {
		payload := collector.buildPayload()
		data, status, err := postJSON(endpoint, payload)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s register failed: %v\n", time.Now().Format(time.RFC3339), err)
			return "", false
		}
		heartbeatCount++
		var response AgentRegistrationResponse
		if err := json.Unmarshal(data, &response); err == nil {
			effective := runtimeConfig.update(response.AgentConfig)
			collector.applyConfig(effective)
		}
		if heartbeatCount == 1 || heartbeatCount%heartbeatLogEvery == 0 {
			fmt.Printf("%s registered %s status=%d gpus=%d (heartbeat #%d)\n",
				time.Now().Format(time.RFC3339), payload.Hostname, status, len(payload.GPUs), heartbeatCount)
		}
		terminalOnce.Do(func() {
			go runTerminalWebSocket(args.server, args, payload.Hostname)
		})
		return payload.Hostname, true
	}

	// one-shot 模式：发一次心跳，处理完待执行任务后退出
	if args.interval <= 0 {
		hostname, ok := sendHeartbeat()
		if ok {
			processTasks(args.server, args, hostname)
		}
		return
	}

	// daemon 模式：心跳与任务执行解耦到独立 goroutine。
	// 长时任务（如 container_data_sync）只阻塞任务 goroutine，心跳不受影响。

	// daemon 模式下启动文件列表 HTTP 服务，供后端直接查询目录而无需 SSH
	if args.filesPort > 0 {
		startAgentFilesServer(args.filesToken, args.dataPath, args.filesPort)
	}

	// 先同步发送第一次心跳，直到成功为止，以获得 hostname。
	var hostname string
	for {
		var ok bool
		hostname, ok = sendHeartbeat()
		if ok {
			break
		}
		time.Sleep(time.Duration(args.interval) * time.Second)
	}

	// 心跳周期由管理端配置热更新；systemd/CLI 参数仅作为离线兜底。
	go func() {
		for {
			time.Sleep(time.Duration(runtimeConfig.get().HeartbeatIntervalSeconds) * time.Second)
			sendHeartbeat()
		}
	}()

	// CPU、内存和 GPU 指标使用独立轻量接口，不触发完整节点同步。
	go func() {
		endpoint := strings.TrimRight(args.server, "/") + "/api/nodes/metrics"
		failures := 0
		for {
			time.Sleep(time.Duration(runtimeConfig.get().MetricsIntervalSeconds) * time.Second)
			_, _, err := postJSON(endpoint, buildMetricsReport(args, hostname))
			if err != nil {
				failures++
				if failures == 1 || failures%30 == 0 {
					fmt.Fprintf(os.Stderr, "%s metrics report failed: %v\n", time.Now().Format(time.RFC3339), err)
				}
			} else {
				failures = 0
			}
		}
	}()

	// 任务 goroutine：每 args.taskPollInterval 秒轮询一次待执行任务。
	// 长时任务会阻塞此 goroutine，但不影响上面的心跳。
	go func() {
		for {
			processTasks(args.server, args, hostname)
			time.Sleep(time.Duration(runtimeConfig.get().TaskPollIntervalSeconds) * time.Second)
		}
	}()

	select {} // 永久阻塞主 goroutine（daemon 模式）
}
