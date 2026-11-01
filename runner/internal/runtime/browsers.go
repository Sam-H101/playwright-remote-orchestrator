package runtime

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

// InstallBrowsers runs `node cli.js install <browsers...>` with PLAYWRIGHT_BROWSERS_PATH (and
// PLAYWRIGHT_DOWNLOAD_HOST when set) on top of the allow-listed environment; its output goes to out.
func InstallBrowsers(ctx context.Context, nodeExe, cli, browsersPath string, browsers []string, downloadHost string, out io.Writer) error {
	if err := os.MkdirAll(browsersPath, 0o755); err != nil {
		return err
	}
	set := map[string]string{"PLAYWRIGHT_BROWSERS_PATH": browsersPath, "NODE_OPTIONS": ""}
	if downloadHost != "" {
		set["PLAYWRIGHT_DOWNLOAD_HOST"] = downloadHost
	}
	cmd := exec.CommandContext(ctx, nodeExe, append([]string{cli, "install"}, browsers...)...)
	cmd.Dir = filepath.Dir(cli)
	cmd.Env = proc.ChildEnv(os.Environ(), set)
	cmd.Stdout, cmd.Stderr = out, out
	proc.Hide(cmd)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("playwright install %v: %w", browsers, err)
	}
	return nil
}
