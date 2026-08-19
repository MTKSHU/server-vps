package main

import (
	"encoding/csv"
	"fmt"
	"math"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func detectNVIDIA() ([]GPUReport, string, string) {
	gpus, driverVersion := detectNVIDIAGPUs()
	return gpus, driverVersion, parseNVIDIACudaVersion()
}

// detectNVIDIAGPUs performs the single query needed for dynamic GPU metrics.
// The slower CUDA Driver API lookup stays on the inventory schedule.
func detectNVIDIAGPUs() ([]GPUReport, string) {
	output := runCommand(
		"nvidia-smi",
		"--query-gpu=index,uuid,name,pci.bus_id,memory.total,memory.used,temperature.gpu,power.draw,utilization.gpu,driver_version",
		"--format=csv,noheader,nounits",
	)
	if output == "" {
		gpus := detectNVIDIAProc()
		return gpus, parseNVIDIADriverVersion()
	}
	var gpus []GPUReport
	driverVersion := ""
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
	return gpus, driverVersion
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
	// v610 renamed the deprecated "CUDA Version" field to "CUDA UMD Version"
	// and may align labels as "CUDA UMD Version : 13.3". Try the normal status
	// page, the detailed query, and --version because driver builds differ in
	// which view exposes the maximum supported CUDA Driver API version.
	for _, args := range [][]string{{}, {"-q"}, {"--version"}} {
		if version := parseNVIDIACudaVersionOutput(
			runCommandTimeout(10*time.Second, "nvidia-smi", args...),
		); version != "" {
			return version
		}
	}
	return ""
}

var nvidiaCUDAVersionRE = regexp.MustCompile(`(?i)CUDA(?:\s+UMD)?\s+Version\s*:\s*([0-9]+(?:\.[0-9]+){1,2})`)

func parseNVIDIACudaVersionOutput(output string) string {
	match := nvidiaCUDAVersionRE.FindStringSubmatch(output)
	if len(match) != 2 {
		return ""
	}
	return match[1]
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
	if len(gpus) == 0 {
		return "unknown"
	}
	hasPascal := false
	hasWorkstation := false
	for _, gpu := range gpus {
		model := strings.ToLower(gpu.Model)
		if isPascalGPU(model) {
			hasPascal = true
		} else if isWorkstationGPU(model) {
			hasWorkstation = true
		}
	}
	switch {
	case hasPascal:
		return "legacy-pascal"
	case hasWorkstation:
		return "workstation"
	default:
		return "modern-geforce"
	}
}

// isPascalGPU reports whether model (lowercase) is a Pascal-architecture GPU
// (CUDA compute capability 6.x). Pascal cards may not be supported by
// newer CUDA/PyTorch builds and are assigned the "legacy-pascal" driver pool.
func isPascalGPU(model string) bool {
	return strings.Contains(model, "gtx 10") || // GTX 1050/1060/1070/1080 series
		strings.Contains(model, "titan xp") ||
		strings.Contains(model, "titan x (pascal)") ||
		strings.Contains(model, "tesla p") || // Tesla P4/P10/P40/P100
		strings.Contains(model, "quadro p") || // Quadro P4000/P5000/P6000
		strings.Contains(model, "quadro gp") // Quadro GP100
}

// isWorkstationGPU reports whether model (lowercase) is a workstation or
// datacenter GPU that warrants the "workstation" driver pool.
func isWorkstationGPU(model string) bool {
	return strings.Contains(model, "a6000") ||
		strings.Contains(model, "a5000") ||
		strings.Contains(model, "a4000") ||
		strings.Contains(model, "a100") ||
		strings.Contains(model, "a40") ||
		strings.Contains(model, "v100") ||
		strings.Contains(model, "h100") ||
		strings.Contains(model, "quadro rtx")
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

func parseIncusContainers(output string) ([]ContainerReport, error) {
	containers := []ContainerReport{}
	if output == "" {
		return containers, nil
	}
	reader := csv.NewReader(strings.NewReader(output))
	reader.FieldsPerRecord = -1
	rows, err := reader.ReadAll()
	if err != nil {
		return nil, err
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
			if report.Name == downloaderContainerName {
				report.Role = "resource_downloader"
			}
			containers = append(containers, report)
		}
	}
	return containers, nil
}

func detectIncusContainersResult() ([]ContainerReport, error) {
	output, err := runCommandCombinedTimeout(10*time.Second, "incus", "list", "--format", "csv", "-c", "ns4")
	if err != nil {
		return nil, err
	}
	return parseIncusContainers(output)
}

func parseIncusImages(output string) ([]IncusImageReport, error) {
	images := []IncusImageReport{}
	if output == "" {
		return images, nil
	}
	reader := csv.NewReader(strings.NewReader(output))
	reader.FieldsPerRecord = -1
	rows, err := reader.ReadAll()
	if err != nil {
		return nil, err
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
	return images, nil
}

func detectIncusImagesResult() ([]IncusImageReport, error) {
	output, err := runCommandCombinedTimeout(30*time.Second, "incus", "image", "list", "--format", "csv", "-c", "lfda")
	if err != nil {
		return nil, err
	}
	return parseIncusImages(output)
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

type payloadCollector struct {
	args            cliArgs
	cached          NodeRegistration
	initialized     bool
	lastContainerAt time.Time
	lastStorageAt   time.Time
	lastInventoryAt time.Time
	now             func() time.Time
}

func newPayloadCollector(args cliArgs) *payloadCollector {
	return &payloadCollector{args: args, now: time.Now}
}

func refreshDue(initialized bool, last, now time.Time, intervalSeconds int) bool {
	if !initialized || last.IsZero() {
		return true
	}
	return !now.Before(last.Add(time.Duration(intervalSeconds) * time.Second))
}

func (collector *payloadCollector) refreshInventory(now time.Time) {
	args := collector.args
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
		if len(gpus) > 0 || !collector.initialized {
			driverPool = inferDriverPool(gpus)
		} else {
			driverPool = collector.cached.DriverPool
		}
	}
	collector.cached.Token = args.token
	collector.cached.Hostname = hostname
	collector.cached.IP = ip
	collector.cached.NodeGroup = args.nodeGroup
	collector.cached.DriverPool = driverPool
	collector.cached.OSVersion = runtime.GOOS
	collector.cached.KernelVersion = runCommand("uname", "-r")
	if driverVersion != "" || !collector.initialized {
		collector.cached.DriverVersion = driverVersion
	}
	if cudaVersion != "" || !collector.initialized {
		collector.cached.CUDADriverAPIVersion = cudaVersion
	}
	collector.cached.IncusStatus = incusStatus()
	collector.cached.AgentVersion = agentVersion
	if len(gpus) > 0 || !collector.initialized {
		collector.cached.GPUs = gpus
	}
	collector.cached.Resources.CPUModel = cpuModel()
	collector.cached.Resources.CPUTotal = runtime.NumCPU()
	collector.cached.Resources.CPUCores = cpuCores()
	collector.cached.Resources.CPUSockets = cpuSockets()
	if images, err := detectIncusImagesResult(); err == nil {
		collector.cached.Images = images
	}
	storageVolumes := detectStorageVolumes(args.dataPath)
	usableStorageSample := false
	for _, volume := range storageVolumes {
		if volume.Exists && volume.Status != "error" {
			usableStorageSample = true
			break
		}
	}
	if usableStorageSample || !collector.initialized {
		collector.cached.StorageVolumes = storageVolumes
	}
	collector.lastInventoryAt = now
}

func (collector *payloadCollector) refreshStorage(now time.Time) {
	total, used := resourceDiskGB(collector.args)
	if total > 0 {
		collector.cached.Resources.DiskTotalGB = total
		collector.cached.Resources.DiskUsedGB = used
	}
	collector.lastStorageAt = now
}

func (collector *payloadCollector) refreshMetrics(skipGPU bool) {
	if !skipGPU {
		if gpus, driverVersion := detectNVIDIAGPUs(); len(gpus) > 0 {
			collector.cached.GPUs = gpus
			if driverVersion != "" {
				collector.cached.DriverVersion = driverVersion
			}
		}
	}
	memoryTotal, memoryUsed := memoryGB()
	swapTotal, swapUsed := swapGB()
	collector.cached.UptimeSeconds = uptimeSeconds()
	collector.cached.Resources.CPUTemperatureC = cpuTemperature()
	collector.cached.Resources.MemoryTotalGB = memoryTotal
	collector.cached.Resources.CPUUsed = 0
	collector.cached.Resources.MemoryUsedGB = memoryUsed
	collector.cached.Resources.LoadAvg = loadAvg()
	collector.cached.Resources.CPUUsagePercent = cpuUsagePercent()
	collector.cached.Resources.SwapTotalGB = swapTotal
	collector.cached.Resources.SwapUsedGB = swapUsed
}

func (collector *payloadCollector) refreshContainers(now time.Time) {
	if containers, err := detectIncusContainersResult(); err == nil {
		collector.cached.Containers = containers
	}
	collector.lastContainerAt = now
}

func (collector *payloadCollector) applyConfig(config AgentCollectionConfig) {
	collector.args.containerInterval = config.ContainerIntervalSeconds
	collector.args.storageInterval = config.StorageIntervalSeconds
	collector.args.inventoryInterval = config.InventoryIntervalSeconds
}

func (collector *payloadCollector) buildPayload() NodeRegistration {
	now := collector.now()
	inventoryDue := refreshDue(collector.initialized, collector.lastInventoryAt, now, collector.args.inventoryInterval)
	if inventoryDue {
		collector.refreshInventory(now)
	}
	if refreshDue(collector.initialized, collector.lastStorageAt, now, collector.args.storageInterval) {
		collector.refreshStorage(now)
	}
	collector.refreshMetrics(inventoryDue)
	if refreshDue(collector.initialized, collector.lastContainerAt, now, collector.args.containerInterval) {
		collector.refreshContainers(now)
	}
	collector.initialized = true
	collector.cached.Capabilities = []string{"managed_nfs_mounts_v1", "typed_mounts_v1", "per_user_nfs_exports_v1", "managed_nfs_hot_mounts_v1"}
	collector.cached.NFSHealth = detectNFSHealth()
	return collector.cached
}

type networkCounters struct {
	Interface string
	RXBytes   uint64
	TXBytes   uint64
	SampledAt time.Time
}

type networkRateSampler struct {
	previous networkCounters
}

func parseDefaultRouteInterface(content string) string {
	selected := ""
	selectedMetric := int64(math.MaxInt64)
	for _, line := range strings.Split(content, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 8 || fields[0] == "Iface" || fields[1] != "00000000" || fields[7] != "00000000" {
			continue
		}
		flags, err := strconv.ParseUint(fields[3], 16, 64)
		if err != nil || flags&1 == 0 {
			continue
		}
		metric, err := strconv.ParseInt(fields[6], 10, 64)
		if err != nil {
			metric = math.MaxInt32
		}
		if selected == "" || metric < selectedMetric {
			selected = fields[0]
			selectedMetric = metric
		}
	}
	return selected
}

func readUintFile(path string) (uint64, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.ParseUint(strings.TrimSpace(string(content)), 10, 64)
}

func readDefaultRouteNetworkCounters(now time.Time) (networkCounters, error) {
	routes, err := os.ReadFile("/proc/net/route")
	if err != nil {
		return networkCounters{}, err
	}
	interfaceName := parseDefaultRouteInterface(string(routes))
	if interfaceName == "" || strings.ContainsAny(interfaceName, `/\\`) {
		return networkCounters{}, fmt.Errorf("IPv4 default route interface not found")
	}
	statisticsPath := filepath.Join("/sys/class/net", interfaceName, "statistics")
	rxBytes, err := readUintFile(filepath.Join(statisticsPath, "rx_bytes"))
	if err != nil {
		return networkCounters{}, err
	}
	txBytes, err := readUintFile(filepath.Join(statisticsPath, "tx_bytes"))
	if err != nil {
		return networkCounters{}, err
	}
	return networkCounters{Interface: interfaceName, RXBytes: rxBytes, TXBytes: txBytes, SampledAt: now}, nil
}

func calculateNetworkRates(previous, current networkCounters) (float64, float64) {
	elapsed := current.SampledAt.Sub(previous.SampledAt).Seconds()
	if previous.Interface == "" || previous.Interface != current.Interface || elapsed <= 0 || current.RXBytes < previous.RXBytes || current.TXBytes < previous.TXBytes {
		return 0, 0
	}
	return float64(current.RXBytes-previous.RXBytes) / elapsed, float64(current.TXBytes-previous.TXBytes) / elapsed
}

func (sampler *networkRateSampler) sample(now time.Time) (string, float64, float64) {
	current, err := readDefaultRouteNetworkCounters(now)
	if err != nil {
		sampler.previous = networkCounters{}
		return "", 0, 0
	}
	rxRate, txRate := calculateNetworkRates(sampler.previous, current)
	sampler.previous = current
	return current.Interface, rxRate, txRate
}

func buildMetricsReport(args cliArgs, hostname string, networkSampler *networkRateSampler) AgentMetricsReport {
	gpus, _ := detectNVIDIAGPUs()
	memoryTotal, memoryUsed := memoryGB()
	swapTotal, swapUsed := swapGB()
	networkInterface := ""
	networkRXRate := 0.0
	networkTXRate := 0.0
	if networkSampler != nil {
		networkInterface, networkRXRate, networkTXRate = networkSampler.sample(time.Now())
	}
	return AgentMetricsReport{
		Token:                   args.token,
		Hostname:                hostname,
		UptimeSeconds:           uptimeSeconds(),
		CPUUsagePercent:         cpuUsagePercent(),
		CPUTemperatureC:         cpuTemperature(),
		MemoryTotalGB:           memoryTotal,
		MemoryUsedGB:            memoryUsed,
		LoadAvg:                 loadAvg(),
		SwapTotalGB:             swapTotal,
		SwapUsedGB:              swapUsed,
		NetworkInterface:        networkInterface,
		NetworkRXBytesPerSecond: networkRXRate,
		NetworkTXBytesPerSecond: networkTXRate,
		GPUs:                    gpus,
	}
}
