package main

import (
	"encoding/csv"
	"fmt"
	"math"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func detectNVIDIA() ([]GPUReport, string, string) {
	output := runCommand(
		"nvidia-smi",
		"--query-gpu=index,uuid,name,pci.bus_id,memory.total,memory.used,temperature.gpu,power.draw,utilization.gpu,driver_version",
		"--format=csv,noheader,nounits",
	)
	if output == "" {
		gpus := detectNVIDIAProc()
		return gpus, parseNVIDIADriverVersion(), parseNVIDIACudaVersion()
	}
	var gpus []GPUReport
	driverVersion := ""
	cudaVersion := parseNVIDIACudaVersion()
	for _, line := range strings.Split(output, "\n") {
		parts := strings.Split(line, ",")
		if len(parts) < 10 {
			continue
		}
		for index := range parts {
			parts[index] = strings.TrimSpace(parts[index])
		}
		if driverVersion == "" {
			driverVersion = parts[9]
		}
		gpus = append(gpus, GPUReport{
			Slot:         parseInt(parts[0]),
			UUID:         parts[1],
			Model:        parts[2],
			PCIAddress:   normalizePCIAddress(parts[3]),
			VRAMGB:       parseInt(parts[4]) / 1024,
			VRAMUsedMB:   parseInt(parts[5]),
			TemperatureC: parseInt(parts[6]),
			PowerW:       parseRoundedInt(parts[7]),
			Utilization:  parseInt(parts[8]),
		})
	}
	return gpus, driverVersion, cudaVersion
}

func detectNVIDIAProc() []GPUReport {
	entries, err := os.ReadDir("/proc/driver/nvidia/gpus")
	if err != nil {
		return []GPUReport{}
	}
	gpus := make([]GPUReport, 0, len(entries))
	for slot, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		content, err := os.ReadFile(filepath.Join("/proc/driver/nvidia/gpus", entry.Name(), "information"))
		if err != nil {
			continue
		}
		info := parseKeyValueInfo(string(content))
		model := info["Model"]
		uuid := info["GPU UUID"]
		if model == "" || uuid == "" {
			continue
		}
		gpus = append(gpus, GPUReport{
			Slot:         slot,
			UUID:         uuid,
			Model:        model,
			PCIAddress:   normalizePCIAddress(info["Bus Location"]),
			VRAMGB:       inferVRAMGB(model),
			TemperatureC: 0,
			PowerW:       0,
			Utilization:  0,
		})
	}
	return gpus
}

func uptimeSeconds() int64 {
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(data))
	if len(fields) == 0 {
		return 0
	}
	value, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return 0
	}
	return int64(value)
}

func normalizePCIAddress(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return ""
	}
	if strings.Count(value, ":") == 1 {
		return "0000:" + value
	}
	return value
}

func parseKeyValueInfo(content string) map[string]string {
	result := map[string]string{}
	for _, line := range strings.Split(content, "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		result[strings.TrimSpace(parts[0])] = strings.TrimSpace(parts[1])
	}
	return result
}

func parseNVIDIADriverVersion() string {
	content, err := os.ReadFile("/proc/driver/nvidia/version")
	if err != nil {
		return ""
	}
	fields := strings.Fields(string(content))
	for index, field := range fields {
		if field == "Module" && index+1 < len(fields) {
			return fields[index+1]
		}
	}
	return ""
}

func parseNVIDIACudaVersion() string {
	output := runCommandTimeout(3*time.Second, "nvidia-smi")
	if output == "" {
		return ""
	}
	marker := "CUDA Version:"
	index := strings.Index(output, marker)
	if index < 0 {
		return ""
	}
	version := strings.TrimSpace(output[index+len(marker):])
	if version == "" {
		return ""
	}
	fields := strings.Fields(version)
	if len(fields) == 0 {
		return ""
	}
	return strings.Trim(fields[0], "|")
}

func parseRoundedInt(value string) int {
	value = strings.TrimSpace(value)
	if value == "" || strings.EqualFold(value, "[not supported]") || strings.EqualFold(value, "N/A") {
		return 0
	}
	if parsed, err := strconv.Atoi(value); err == nil {
		return parsed
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return 0
	}
	return int(math.Round(parsed))
}

func inferVRAMGB(model string) int {
	normalized := strings.ToLower(model)
	switch {
	case strings.Contains(normalized, "tesla p40"):
		return 24
	case strings.Contains(normalized, "a6000"):
		return 48
	case strings.Contains(normalized, "4090"), strings.Contains(normalized, "3090"), strings.Contains(normalized, "titan rtx"):
		return 24
	case strings.Contains(normalized, "titan xp"):
		return 12
	default:
		return 0
	}
}

func inferDriverPool(gpus []GPUReport) string {
	models := strings.ToLower(fmt.Sprintf("%v", gpus))
	switch {
	case strings.Contains(models, "titan xp"):
		return "legacy-pascal"
	case strings.Contains(models, "a6000"):
		return "workstation"
	case len(gpus) > 0:
		return "modern-geforce"
	default:
		return "unknown"
	}
}

func memoryGB() (int, int) {
	content, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 1, 0
	}
	totalKB := 0
	availableKB := 0
	for _, line := range strings.Split(string(content), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		switch fields[0] {
		case "MemTotal:":
			totalKB, _ = strconv.Atoi(fields[1])
		case "MemAvailable:":
			availableKB, _ = strconv.Atoi(fields[1])
		}
	}
	totalGB := totalKB / 1024 / 1024
	usedGB := (totalKB - availableKB) / 1024 / 1024
	if totalGB < 1 {
		totalGB = 1
	}
	return totalGB, usedGB
}

func diskGB(path string) (float64, float64) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 1, 0
	}
	total := float64(stat.Blocks*uint64(stat.Bsize)) / 1024 / 1024 / 1024
	free := float64(stat.Bavail*uint64(stat.Bsize)) / 1024 / 1024 / 1024
	return total, total - free
}

func parseStorageSizeGB(value string) float64 {
	value = strings.TrimSpace(strings.TrimSuffix(value, "."))
	if value == "" {
		return 0
	}
	fields := strings.Fields(value)
	rawNumber := ""
	unit := ""
	if len(fields) >= 1 {
		rawNumber = fields[0]
	}
	if len(fields) >= 2 {
		unit = fields[1]
	}
	if rawNumber == "" {
		return 0
	}
	index := 0
	for index < len(rawNumber) {
		ch := rawNumber[index]
		if (ch >= '0' && ch <= '9') || ch == '.' {
			index++
			continue
		}
		break
	}
	if index < len(rawNumber) {
		unit = rawNumber[index:] + unit
		rawNumber = rawNumber[:index]
	}
	number, err := strconv.ParseFloat(strings.TrimSpace(rawNumber), 64)
	if err != nil || number <= 0 {
		return 0
	}
	switch strings.ToLower(strings.TrimSpace(unit)) {
	case "b", "byte", "bytes":
		number = number / 1024 / 1024 / 1024
	case "kb", "kib":
		number = number / 1024 / 1024
	case "mb", "mib":
		number = number / 1024
	case "", "gb", "gib":
	case "tb", "tib":
		number = number * 1024
	case "pb", "pib":
		number = number * 1024 * 1024
	default:
		return 0
	}
	return number
}

func parseIncusStorageInfoGB(output string) (totalGB float64, usedGB float64) {
	for _, line := range strings.Split(output, "\n") {
		parts := strings.SplitN(strings.TrimSpace(line), ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.ToLower(strings.TrimSpace(parts[0]))
		value := strings.TrimSpace(parts[1])
		switch key {
		case "space used":
			usedGB = parseStorageSizeGB(value)
		case "total space":
			totalGB = parseStorageSizeGB(value)
		}
	}
	return totalGB, usedGB
}

func incusStoragePoolGB(pool string) (totalGB float64, usedGB float64) {
	pool = strings.TrimSpace(pool)
	if pool == "" {
		pool = detectIncusStoragePool()
	}
	if pool == "" {
		return 0, 0
	}
	output := runCommandTimeout(5*time.Second, "incus", "storage", "info", pool)
	return parseIncusStorageInfoGB(output)
}

func resourceDiskGB(args cliArgs) (float64, float64) {
	totalGB, usedGB := incusStoragePoolGB(args.incusStoragePool)
	if totalGB > 0 {
		return totalGB, usedGB
	}
	return diskGB(args.dataPath)
}

func storageStat(path string) (totalGB int, usedGB int, freeGB int, err error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, 0, 0, err
	}
	totalGB = int((stat.Blocks * uint64(stat.Bsize)) / 1024 / 1024 / 1024)
	freeGB = int((stat.Bavail * uint64(stat.Bsize)) / 1024 / 1024 / 1024)
	usedGB = totalGB - freeGB
	return totalGB, usedGB, freeGB, nil
}

func directoryUsedGB(path string) int {
	output := runCommandTimeout(5*time.Second, "du", "-s", "-B1", path)
	fields := strings.Fields(output)
	if len(fields) == 0 {
		return 0
	}
	bytesUsed, err := strconv.ParseUint(fields[0], 10, 64)
	if err != nil {
		return 0
	}
	return int(bytesUsed / 1024 / 1024 / 1024)
}

func storageVolume(name string, path string) StorageVolume {
	volume := StorageVolume{
		Name:   name,
		Path:   filepath.Clean(path),
		Status: "ok",
	}
	if _, err := os.Stat(volume.Path); err != nil {
		volume.Exists = false
		volume.Status = "missing"
		volume.Error = err.Error()
		return volume
	}
	volume.Exists = true
	totalGB, usedGB, freeGB, err := storageStat(volume.Path)
	if err != nil {
		volume.Status = "error"
		volume.Error = err.Error()
		return volume
	}
	volume.TotalGB = totalGB
	volume.UsedGB = usedGB
	volume.FreeGB = freeGB
	volume.DirectoryUsedGB = directoryUsedGB(volume.Path)
	if totalGB > 0 && freeGB*100/totalGB < 10 {
		volume.Status = "warning"
	}
	return volume
}

func detectStorageVolumes(dataPath string) []StorageVolume {
	base := filepath.Clean(dataPath)
	if base == "." || base == "/" || base == "" {
		base = "/data"
	}
	paths := []struct {
		name string
		path string
	}{
		{"root", base},
		{"users", filepath.Join(base, "users")},
		{"datasets", filepath.Join(base, "datasets")},
		{"models", filepath.Join(base, "models")},
		{"backups", filepath.Join(base, "backups")},
	}
	volumes := make([]StorageVolume, 0, len(paths))
	for _, item := range paths {
		volumes = append(volumes, storageVolume(item.name, item.path))
	}
	return volumes
}

func loadAvg() float64 {
	content, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(content))
	if len(fields) == 0 {
		return 0
	}
	value, _ := strconv.ParseFloat(fields[0], 64)
	return value
}

func swapGB() (totalGB float64, usedGB float64) {
	content, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, 0
	}
	var swapTotalKB, swapFreeKB int
	for _, line := range strings.Split(string(content), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		switch fields[0] {
		case "SwapTotal:":
			swapTotalKB, _ = strconv.Atoi(fields[1])
		case "SwapFree:":
			swapFreeKB, _ = strconv.Atoi(fields[1])
		}
	}
	totalGB = float64(swapTotalKB) / 1024 / 1024
	usedGB = float64(swapTotalKB-swapFreeKB) / 1024 / 1024
	return totalGB, usedGB
}

func cpuUsagePercent() float64 {
	// 读两次 /proc/stat，间隔 200ms 采样
	readStat := func() (idle, total uint64) {
		content, err := os.ReadFile("/proc/stat")
		if err != nil {
			return 0, 0
		}
		for _, line := range strings.Split(string(content), "\n") {
			if !strings.HasPrefix(line, "cpu ") {
				continue
			}
			fields := strings.Fields(line)
			if len(fields) < 5 {
				break
			}
			for i, f := range fields[1:] {
				v, _ := strconv.ParseUint(f, 10, 64)
				total += v
				if i == 3 { // idle
					idle = v
				}
			}
			break
		}
		return idle, total
	}
	idle1, total1 := readStat()
	time.Sleep(200 * time.Millisecond)
	idle2, total2 := readStat()
	totalDiff := total2 - total1
	idleDiff := idle2 - idle1
	if totalDiff == 0 {
		return 0
	}
	return float64(totalDiff-idleDiff) / float64(totalDiff) * 100
}

func cpuModel() string {
	if model := cpuModelFromCPUInfo(); model != "" {
		return model
	}
	if model := cpuModelFromLSCPU(); model != "" {
		return model
	}
	return ""
}

func cpuSockets() int {
	// 从 /proc/cpuinfo 读取 physical id 的数量
	content, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return 1
	}
	sockets := map[string]struct{}{}
	for _, line := range strings.Split(string(content), "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		if strings.TrimSpace(strings.ToLower(parts[0])) == "physical id" {
			id := strings.TrimSpace(parts[1])
			if id != "" {
				sockets[id] = struct{}{}
			}
		}
	}
	if len(sockets) == 0 {
		return 1
	}
	return len(sockets)
}

func cpuCores() int {
	// 从 /proc/cpuinfo 读取 cpu cores 字段
	content, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return runtime.NumCPU()
	}
	for _, line := range strings.Split(string(content), "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		if strings.TrimSpace(strings.ToLower(parts[0])) == "cpu cores" {
			cores, err := strconv.Atoi(strings.TrimSpace(parts[1]))
			if err == nil && cores > 0 {
				return cores
			}
		}
	}
	return runtime.NumCPU()
}

func cpuTemperature() int {
	// 遍历 /sys/class/hwmon/ 读取 CPU 温度
	entries, err := os.ReadDir("/sys/class/hwmon")
	if err != nil {
		return 0
	}
	for _, entry := range entries {
		base := filepath.Join("/sys/class/hwmon", entry.Name())
		// 检查 name 文件确认是 CPU 传感器
		nameBytes, err := os.ReadFile(filepath.Join(base, "name"))
		if err != nil {
			continue
		}
		name := strings.TrimSpace(string(nameBytes))
		if !strings.Contains(strings.ToLower(name), "coretemp") &&
			!strings.Contains(strings.ToLower(name), "k10temp") &&
			!strings.Contains(strings.ToLower(name), "cpu") {
			continue
		}
		// 读取 temp*_input 获取温度（毫摄氏度）
		hwEntries, err := os.ReadDir(base)
		if err != nil {
			continue
		}
		maxTemp := 0
		for _, he := range hwEntries {
			if !strings.HasPrefix(he.Name(), "temp") || !strings.HasSuffix(he.Name(), "_input") {
				continue
			}
			tempBytes, err := os.ReadFile(filepath.Join(base, he.Name()))
			if err != nil {
				continue
			}
			tempMilli, err := strconv.Atoi(strings.TrimSpace(string(tempBytes)))
			if err != nil {
				continue
			}
			tempC := tempMilli / 1000
			if tempC > maxTemp {
				maxTemp = tempC
			}
		}
		if maxTemp > 0 {
			return maxTemp
		}
	}
	return 0
}

func cpuModelFromCPUInfo() string {
	content, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return ""
	}
	values := map[string]string{}
	for _, line := range strings.Split(string(content), "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(strings.ToLower(parts[0]))
		value := strings.TrimSpace(parts[1])
		if value != "" && values[key] == "" {
			values[key] = value
		}
	}
	for _, key := range []string{"model name", "hardware", "cpu", "vendor_id", "processor"} {
		if value := cleanCPUModel(values[key]); value != "" {
			return value
		}
	}
	return ""
}

func cpuModelFromLSCPU() string {
	output := runCommand("lscpu")
	info := parseKeyValueInfo(output)
	for _, key := range []string{"Model name", "BIOS Model name", "Vendor ID"} {
		if value := cleanCPUModel(info[key]); value != "" {
			return value
		}
	}
	return ""
}

func cleanCPUModel(value string) string {
	value = strings.Join(strings.Fields(strings.TrimSpace(value)), " ")
	if value == "" {
		return ""
	}
	if _, err := strconv.Atoi(value); err == nil {
		return ""
	}
	if strings.EqualFold(value, "unknown") {
		return ""
	}
	return value
}

func parseCPUModelForTest(content string) string {
	values := map[string]string{}
	for _, line := range strings.Split(content, "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(strings.ToLower(parts[0]))
		value := strings.TrimSpace(parts[1])
		if value != "" && values[key] == "" {
			values[key] = value
		}
	}
	for _, key := range []string{"model name", "hardware", "cpu", "vendor_id", "processor"} {
		if value := cleanCPUModel(values[key]); value != "" {
			return value
		}
	}
	return ""
}

func incusStatus() string {
	// 尝试获取版本号：incus version 输出如 "Client version: 6.1.0\nServer version: 6.1.0"
	output := runCommandTimeout(5*time.Second, "incus", "version")
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(strings.ToLower(line), "server version:") {
			parts := strings.SplitN(line, ":", 2)
			if len(parts) == 2 {
				ver := strings.TrimSpace(parts[1])
				if ver != "" {
					return ver
				}
			}
		}
	}
	// fallback: 如果 incus info 能运行则标 ready，否则 unavailable
	if runCommand("incus", "info") != "" {
		return "ready"
	}
	return "unavailable"
}

func parseFirstIPv4(value string) string {
	for _, field := range strings.FieldsFunc(value, func(r rune) bool {
		return r == ' ' || r == '\n' || r == '\t' || r == ',' || r == '(' || r == ')'
	}) {
		ip := net.ParseIP(field)
		if ip != nil && ip.To4() != nil && !ip.IsLoopback() {
			return ip.String()
		}
	}
	return ""
}

func detectIncusContainers() []ContainerReport {
	containers := []ContainerReport{}
	output := runCommand("incus", "list", "--format", "csv", "-c", "ns4")
	if output == "" {
		return containers
	}
	reader := csv.NewReader(strings.NewReader(output))
	reader.FieldsPerRecord = -1
	rows, err := reader.ReadAll()
	if err != nil {
		return containers
	}
	for _, row := range rows {
		if len(row) < 2 {
			continue
		}
		report := ContainerReport{
			Name:   strings.TrimSpace(row[0]),
			Status: strings.ToLower(strings.TrimSpace(row[1])),
		}
		if len(row) >= 3 {
			report.IP = parseFirstIPv4(strings.Join(row[2:], ","))
		}
		if report.Name != "" {
			containers = append(containers, report)
		}
	}
	return containers
}

func detectIncusImages() []IncusImageReport {
	images := []IncusImageReport{}
	output := runCommand("incus", "image", "list", "--format", "csv", "-c", "lfda")
	if output == "" {
		return images
	}
	reader := csv.NewReader(strings.NewReader(output))
	reader.FieldsPerRecord = -1
	rows, err := reader.ReadAll()
	if err != nil {
		return images
	}
	for _, row := range rows {
		if len(row) < 2 {
			continue
		}
		image := IncusImageReport{
			Aliases:     strings.TrimSpace(row[0]),
			Fingerprint: strings.TrimSpace(row[1]),
		}
		if len(row) >= 3 {
			image.Description = strings.TrimSpace(row[2])
		}
		if len(row) >= 4 {
			image.Architecture = strings.TrimSpace(row[3])
		}
		if image.Fingerprint != "" {
			images = append(images, image)
		}
	}
	return images
}

func detectIncusStoragePool() string {
	pool := runCommand("incus", "profile", "device", "get", "default", "root", "pool")
	if pool != "" {
		return pool
	}
	output := runCommand("incus", "storage", "list", "--format", "csv", "-c", "n")
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			return line
		}
	}
	return ""
}

func buildPayload(args cliArgs) NodeRegistration {
	gpus, driverVersion, cudaVersion := detectNVIDIA()
	hostname := args.hostname
	if hostname == "" {
		hostname, _ = os.Hostname()
	}
	ip := args.ip
	if ip == "" {
		ip = detectIP()
	}
	driverPool := args.driverPool
	if driverPool == "" {
		driverPool = inferDriverPool(gpus)
	}
	memoryTotal, memoryUsed := memoryGB()
	diskTotal, diskUsed := resourceDiskGB(args)
	swapTotal, swapUsed := swapGB()
	return NodeRegistration{
		Token:                args.token,
		Hostname:             hostname,
		IP:                   ip,
		NodeGroup:            args.nodeGroup,
		DriverPool:           driverPool,
		OSVersion:            runtime.GOOS,
		KernelVersion:        runCommand("uname", "-r"),
		DriverVersion:        driverVersion,
		CUDADriverAPIVersion: cudaVersion,
		IncusStatus:          incusStatus(),
		AgentVersion:         agentVersion,
		UptimeSeconds:        uptimeSeconds(),
		Resources: ResourceReport{
			CPUModel:        cpuModel(),
			CPUTotal:        runtime.NumCPU(),
			CPUCores:        cpuCores(),
			CPUSockets:      cpuSockets(),
			CPUTemperatureC: cpuTemperature(),
			MemoryTotalGB:   memoryTotal,
			DiskTotalGB:     diskTotal,
			CPUUsed:         0,
			MemoryUsedGB:    memoryUsed,
			DiskUsedGB:      diskUsed,
			LoadAvg:         loadAvg(),
			CPUUsagePercent: cpuUsagePercent(),
			SwapTotalGB:     swapTotal,
			SwapUsedGB:      swapUsed,
		},
		GPUs:           gpus,
		Containers:     detectIncusContainers(),
		Images:         detectIncusImages(),
		StorageVolumes: detectStorageVolumes(args.dataPath),
	}
}
