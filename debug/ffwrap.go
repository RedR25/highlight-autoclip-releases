// Temporary: stands in for ffmpeg.exe (IMAGEIO_FFMPEG_EXE) and logs each call's arguments, exit code and stderr
// to FFLOG, then exits with ffmpeg's own code. stdin and stdout are ffmpeg's own handles.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
)

func main() {
	cmd := exec.Command(os.Getenv("REAL_FFMPEG"), os.Args[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000} // CREATE_NO_WINDOW, as the app starts it
	cmd.Stdin, cmd.Stdout = os.Stdin, os.Stdout
	var errbuf bytes.Buffer
	cmd.Stderr = io.MultiWriter(os.Stderr, &errbuf)
	start := time.Now()
	err := cmd.Run()
	code := -1
	if cmd.ProcessState != nil {
		code = cmd.ProcessState.ExitCode()
	}
	msg := ""
	if err != nil {
		msg = err.Error()
	}
	rec, _ := json.Marshal(map[string]any{"args": os.Args[1:], "code": code, "hex": fmt.Sprintf("0x%08X", uint32(code)),
		"stderr": errbuf.String(), "secs": time.Since(start).Seconds(), "err": msg, "start": start.Format(time.RFC3339Nano)})
	os.WriteFile(filepath.Join(os.Getenv("FFLOG"), fmt.Sprintf("%d-%d.json", start.UnixNano(), os.Getpid())), rec, 0o644)
	if code == -1 {
		code = 1
	}
	os.Exit(code)
}
