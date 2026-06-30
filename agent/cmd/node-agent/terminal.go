package main

import (
	"fmt"
	"github.com/creack/pty"
	"github.com/gorilla/websocket"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

type TerminalMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id,omitempty"`
	Container string `json:"container,omitempty"`
	User      string `json:"user,omitempty"`
	Data      string `json:"data,omitempty"`
	Cols      int    `json:"cols,omitempty"`
	Rows      int    `json:"rows,omitempty"`
	Error     string `json:"error,omitempty"`
	ExitCode  int    `json:"exit_code,omitempty"`
}

type TerminalSession struct {
	id   string
	cmd  *exec.Cmd
	file *os.File
}

var terminalSessions = struct {
	sync.Mutex
	items map[string]*TerminalSession
}{items: map[string]*TerminalSession{}}

func websocketURL(server string, hostname string) string {
	parsed, err := url.Parse(strings.TrimRight(server, "/") + "/api/agent/terminal")
	if err != nil {
		return ""
	}
	if parsed.Scheme == "https" {
		parsed.Scheme = "wss"
	} else {
		parsed.Scheme = "ws"
	}
	query := parsed.Query()
	query.Set("hostname", hostname)
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

func sendTerminalMessage(conn *websocket.Conn, writeMu *sync.Mutex, message TerminalMessage) error {
	writeMu.Lock()
	defer writeMu.Unlock()
	return conn.WriteJSON(message)
}

func terminalCommand(message TerminalMessage) *exec.Cmd {
	if message.Container == "" {
		shell := os.Getenv("SHELL")
		if shell == "" {
			shell = "/bin/bash"
		}
		return exec.Command(shell, "-l")
	}
	return exec.Command("incus", "exec", message.Container, "--", "su", "-l", message.User)
}

func startTerminalSession(conn *websocket.Conn, writeMu *sync.Mutex, message TerminalMessage) {
	if message.SessionID == "" {
		_ = sendTerminalMessage(conn, writeMu, TerminalMessage{Type: "error", SessionID: message.SessionID, Error: "invalid terminal start message"})
		return
	}
	cmd := terminalCommand(message)
	cmd.Env = append(os.Environ(), "TERM=xterm-256color")
	rows := uint16(message.Rows)
	cols := uint16(message.Cols)
	if rows == 0 {
		rows = 32
	}
	if cols == 0 {
		cols = 100
	}
	file, err := pty.StartWithSize(cmd, &pty.Winsize{Rows: rows, Cols: cols})
	if err != nil {
		_ = sendTerminalMessage(conn, writeMu, TerminalMessage{Type: "error", SessionID: message.SessionID, Error: err.Error()})
		return
	}
	session := &TerminalSession{id: message.SessionID, cmd: cmd, file: file}
	terminalSessions.Lock()
	terminalSessions.items[message.SessionID] = session
	terminalSessions.Unlock()
	_ = sendTerminalMessage(conn, writeMu, TerminalMessage{Type: "started", SessionID: message.SessionID})
	go func() {
		buffer := make([]byte, 4096)
		for {
			n, err := file.Read(buffer)
			if n > 0 {
				_ = sendTerminalMessage(conn, writeMu, TerminalMessage{
					Type:      "data",
					SessionID: message.SessionID,
					Data:      string(buffer[:n]),
				})
			}
			if err != nil {
				break
			}
		}
		_ = cmd.Wait()
		terminalSessions.Lock()
		delete(terminalSessions.items, message.SessionID)
		terminalSessions.Unlock()
		_ = file.Close()
		_ = sendTerminalMessage(conn, writeMu, TerminalMessage{Type: "exit", SessionID: message.SessionID})
	}()
}

func terminalSession(message TerminalMessage) *TerminalSession {
	terminalSessions.Lock()
	defer terminalSessions.Unlock()
	return terminalSessions.items[message.SessionID]
}

func closeTerminalSession(sessionID string) {
	terminalSessions.Lock()
	session := terminalSessions.items[sessionID]
	delete(terminalSessions.items, sessionID)
	terminalSessions.Unlock()
	if session != nil {
		_ = session.file.Close()
		if session.cmd.Process != nil {
			_ = session.cmd.Process.Kill()
		}
	}
}

func runTerminalWebSocket(server string, args cliArgs, hostname string) {
	for {
		endpoint := websocketURL(server, hostname)
		if endpoint == "" {
			time.Sleep(10 * time.Second)
			continue
		}
		header := http.Header{}
		header.Set("Authorization", "Bearer "+args.token)
		conn, _, err := websocket.DefaultDialer.Dial(endpoint, header)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s terminal websocket failed: %v\n", time.Now().Format(time.RFC3339), err)
			time.Sleep(5 * time.Second)
			continue
		}
		fmt.Printf("%s terminal websocket connected\n", time.Now().Format(time.RFC3339))
		writeMu := &sync.Mutex{}
		for {
			var message TerminalMessage
			if err := conn.ReadJSON(&message); err != nil {
				fmt.Fprintf(os.Stderr, "%s terminal websocket closed: %v\n", time.Now().Format(time.RFC3339), err)
				_ = conn.Close()
				break
			}
			switch message.Type {
			case "start":
				startTerminalSession(conn, writeMu, message)
			case "input":
				if session := terminalSession(message); session != nil {
					_, _ = session.file.Write([]byte(message.Data))
				}
			case "resize":
				if session := terminalSession(message); session != nil && message.Cols > 0 && message.Rows > 0 {
					_ = pty.Setsize(session.file, &pty.Winsize{Rows: uint16(message.Rows), Cols: uint16(message.Cols)})
				}
			case "close":
				closeTerminalSession(message.SessionID)
			}
		}
		time.Sleep(2 * time.Second)
	}
}
