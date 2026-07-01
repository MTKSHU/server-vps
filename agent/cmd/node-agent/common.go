package main

import (
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
