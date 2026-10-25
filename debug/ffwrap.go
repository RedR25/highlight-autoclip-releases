// Temporary: stands in for ffmpeg.exe (IMAGEIO_FFMPEG_EXE) and logs each call's arguments, exit code and stderr
// to FFLOG, then exits with ffmpeg's own code. stdin and stdout are ffmpeg's own handles.
// FAKE_NVENC=early (a simulation, for the app's fallback): the h264_nvenc probe "passes", and every h264_nvenc piece reads
// one frame and fails at encoder start, as an NVENC session error would.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func record(start time.Time, args []string, code int, stderr, msg string) {
	rec, _ := json.Marshal(map[string]any{"args": args, "code": code, "hex": fmt.Sprintf("0x%08X", uint32(code)),
		"stderr": stderr, "secs": time.Since(start).Seconds(), "err": msg, "start": start.Format(time.RFC3339Nano)})
	os.WriteFile(filepath.Join(os.Getenv("FFLOG"), fmt.Sprintf("%d-%d.json", start.UnixNano(), os.Getpid())), rec, 0o644)
}

func main() {
	args, start := os.Args[1:], time.Now()
	joined := strings.Join(args, " ")
	if os.Getenv("FAKE_NVENC") == "early" && strings.Contains(joined, "h264_nvenc") {
		if strings.Contains(joined, "testsrc2=s=1280x720:r=30:d=0.3") {
			record(start, args, 0, "", "fake probe pass")
			os.Exit(0)
		}
		for i, a := range args { // one yuva420p frame of the first input
			if a == "-s" && i+1 < len(args) {
				var w, h int
				fmt.Sscanf(args[i+1], "%dx%d", &w, &h)
				io.CopyN(io.Discard, os.Stdin, int64(w*h*5/2))
				break
			}
		}
		e := "[h264_nvenc @ 0000000000000000] OpenEncodeSessionEx failed: out of memory (10): (no details)\n"
		os.Stderr.WriteString(e)
		record(start, args, -1, e, "fake nvenc failure")
		os.Exit(-1)
	}
	cmd := exec.Command(os.Getenv("REAL_FFMPEG"), args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000} // CREATE_NO_WINDOW, as the app starts it
	cmd.Stdin, cmd.Stdout = os.Stdin, os.Stdout
	var errbuf bytes.Buffer
	cmd.Stderr = io.MultiWriter(os.Stderr, &errbuf)
	err := cmd.Run()
	code := -1
	if cmd.ProcessState != nil {
		code = cmd.ProcessState.ExitCode()
	}
	msg := ""
	if err != nil {
		msg = err.Error()
	}
	record(start, args, code, errbuf.String(), msg)
	if code == -1 {
		code = 1
	}
	os.Exit(code)
}
