package main

import (
	"reflect"
	"testing"
)

func TestTerminalCommandUsesContainerUser(t *testing.T) {
	cmd := terminalCommand(TerminalMessage{Container: "demo", User: "alice"})
	want := []string{"incus", "exec", "demo", "--", "su", "-l", "alice"}
	if !reflect.DeepEqual(cmd.Args, want) {
		t.Fatalf("terminal command args = %#v, want %#v", cmd.Args, want)
	}
}
