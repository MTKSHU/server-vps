package main

import (
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type cliArgs struct {
	server           string
	token            string
	interval         int
	hostname         string
	ip               string
	nodeGroup        string
	driverPool       string
	dataPath         string
	incusStoragePool string
	taskPollInterval int
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

	if args.server == "" || args.token == "" {
		fmt.Fprintln(os.Stderr, "--server and --token are required")
		os.Exit(2)
	}
	endpoint := strings.TrimRight(args.server, "/") + "/api/nodes/register"
	var terminalOnce sync.Once
	heartbeatCount := 0
	const heartbeatLogEvery = 10 // 每 10 次心跳才打印一次成功日志

	// sendHeartbeat 发送一次心跳，返回 (hostname, 是否成功)。
	// 只在心跳 goroutine 内调用，无并发竞争。
	sendHeartbeat := func() (string, bool) {
		payload := buildPayload(args)
		_, status, err := postJSON(endpoint, payload)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s register failed: %v\n", time.Now().Format(time.RFC3339), err)
			return "", false
		}
		heartbeatCount++
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

	// 心跳 goroutine：每 args.interval 秒发一次心跳，与任务执行完全独立
	go func() {
		ticker := time.NewTicker(time.Duration(args.interval) * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			sendHeartbeat()
		}
	}()

	// 任务 goroutine：每 args.taskPollInterval 秒轮询一次待执行任务。
	// 长时任务会阻塞此 goroutine，但不影响上面的心跳。
	go func() {
		processTasks(args.server, args, hostname) // 启动后立即执行一次
		ticker := time.NewTicker(time.Duration(args.taskPollInterval) * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			processTasks(args.server, args, hostname)
		}
	}()

	select {} // 永久阻塞主 goroutine（daemon 模式）
}
