package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

func runCommand(name string, args ...string) string {
	cmd := exec.Command(name, args...)
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(output))
}

func runCommandTimeout(timeout time.Duration, name string, args ...string) string {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	output, err := cmd.Output()
	if err != nil || ctx.Err() == context.DeadlineExceeded {
		return ""
	}
	return strings.TrimSpace(string(output))
}

func runCommandCombinedTimeout(timeout time.Duration, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	output, err := cmd.CombinedOutput()
	text := strings.TrimSpace(string(output))
	if ctx.Err() == context.DeadlineExceeded {
		return text, fmt.Errorf("%s timed out after %v", name, timeout)
	}
	if err != nil {
		if text == "" {
			text = err.Error()
		}
		return text, fmt.Errorf("%s %s failed: %s", name, strings.Join(args, " "), text)
	}
	return text, nil
}

func runCommandCombined(name string, args ...string) (string, error) {
	return runCommandCombinedTimeout(10*time.Minute, name, args...)
}

func detectIP() string {
	conn, err := net.DialTimeout("udp", "8.8.8.8:80", time.Second)
	if err != nil {
		return "127.0.0.1"
	}
	defer conn.Close()
	local := conn.LocalAddr().(*net.UDPAddr)
	return local.IP.String()
}

func parseInt(value string) int {
	value = strings.TrimSpace(strings.TrimSuffix(value, "W"))
	value = strings.TrimSpace(strings.TrimSuffix(value, "%"))
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return 0
	}
	return int(parsed)
}

func postJSON(url string, payload any) ([]byte, int, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, err
	}
	client := &http.Client{Timeout: 8 * time.Second}
	response, err := client.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, response.StatusCode, err
	}
	if response.StatusCode >= 400 {
		return data, response.StatusCode, fmt.Errorf("server returned HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(data)))
	}
	return data, response.StatusCode, nil
}

func claimTask(server string, args cliArgs, hostname string) (*AgentTask, error) {
	endpoint := strings.TrimRight(server, "/") + "/api/nodes/tasks/claim"
	data, _, err := postJSON(endpoint, TaskClaimRequest{Token: args.token, Hostname: hostname})
	if err != nil {
		return nil, err
	}
	var envelope TaskEnvelope
	if err := json.Unmarshal(data, &envelope); err != nil {
		return nil, err
	}
	return envelope.Task, nil
}

func reportTask(server string, args cliArgs, hostname string, taskID int, result TaskResultRequest) error {
	result.Token = args.token
	result.Hostname = hostname
	endpoint := fmt.Sprintf("%s/api/nodes/tasks/%d/result", strings.TrimRight(server, "/"), taskID)
	_, _, err := postJSON(endpoint, result)
	return err
}

// reportTaskProgress 上报数据同步任务的实时进度（best-effort，失败静默忽略）。
func reportTaskProgress(server string, args cliArgs, hostname string, taskID int, progress SyncProgress) {
	endpoint := fmt.Sprintf("%s/api/nodes/tasks/%d/progress", strings.TrimRight(server, "/"), taskID)
	_, _, _ = postJSON(endpoint, TaskProgressRequest{Token: args.token, Hostname: hostname, Progress: progress})
}

// scanLinesCR 是 bufio.Scanner 的分割函数，同时以 \n 与 \r 作为分隔符。
// rsync 的 --info=progress2 进度行以 \r 结尾刷新，需要这样才能实时读取。
func scanLinesCR(data []byte, atEOF bool) (advance int, token []byte, err error) {
	if atEOF && len(data) == 0 {
		return 0, nil, nil
	}
	for i, b := range data {
		if b == '\n' || b == '\r' {
			return i + 1, data[:i], nil
		}
	}
	if atEOF {
		return len(data), data, nil
	}
	return 0, nil, nil
}

// runCommandWithProgress 运行命令并流式读取其 stdout，逐行解析 rsync 进度行，
// 通过 onProgress 回调实时上报。进度行不计入返回的合并输出，避免污染日志。
func runCommandWithProgress(timeout time.Duration, onProgress func(SyncProgress), name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	var stderrBuf bytes.Buffer
	cmd.Stderr = &stderrBuf
	if err := cmd.Start(); err != nil {
		return "", err
	}
	var outBuf strings.Builder
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	scanner.Split(scanLinesCR)
	for scanner.Scan() {
		line := scanner.Text()
		if p, ok := parseRsyncProgress(line); ok {
			if onProgress != nil {
				onProgress(p)
			}
			continue
		}
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		outBuf.WriteString(trimmed)
		outBuf.WriteString("\n")
	}
	waitErr := cmd.Wait()
	text := strings.TrimSpace(outBuf.String() + stderrBuf.String())
	if ctx.Err() == context.DeadlineExceeded {
		return text, fmt.Errorf("%s timed out after %v", name, timeout)
	}
	if waitErr != nil {
		if text == "" {
			text = waitErr.Error()
		}
		return text, fmt.Errorf("%s %s failed: %s", name, strings.Join(args, " "), text)
	}
	return text, nil
}

// parseRsyncProgress 解析 rsync --info=progress2 的进度行，形如：
//
//	1,234,567  45%    1.23MB/s    0:00:12
//
// 返回已传字节、百分比、速率；无法识别时 ok=false。
func parseRsyncProgress(line string) (SyncProgress, bool) {
	fields := strings.Fields(strings.TrimSpace(line))
	if len(fields) < 3 || !strings.HasSuffix(fields[1], "%") {
		return SyncProgress{}, false
	}
	pct, err := strconv.Atoi(strings.TrimSuffix(fields[1], "%"))
	if err != nil {
		return SyncProgress{}, false
	}
	bytesDone, err := strconv.ParseInt(strings.ReplaceAll(fields[0], ",", ""), 10, 64)
	if err != nil {
		return SyncProgress{}, false
	}
	var bytesTotal int64
	if pct > 0 {
		bytesTotal = bytesDone * 100 / int64(pct)
	}
	return SyncProgress{
		Phase:      "running",
		Pct:        pct,
		BytesDone:  bytesDone,
		BytesTotal: bytesTotal,
		Rate:       fields[2],
	}, true
}
