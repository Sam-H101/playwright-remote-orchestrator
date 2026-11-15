//go:build !windows

package agent

import (
	"os/exec"
	"testing"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/config"
	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/proc"
)

func TestRunServerAsUser(t *testing.T) {
	s := config.Settings{Slots: 2, RunAs: []config.RunAs{{Name: "pwuser1", UID: 1001, GID: 1002, Home: "/home/pwuser1"}, {Name: "pwuser2", UID: 1003, GID: 1004, Home: "/home/pwuser2"}}}
	for i, want := range [][2]uint32{{1001, 1002}, {1003, 1004}} {
		cmd := exec.Command("x")
		proc.Hide(cmd)
		if err := runServerAs(s, i+1)(cmd); err != nil {
			t.Fatal(err)
		}
		c := cmd.SysProcAttr.Credential
		if c == nil || c.Uid != want[0] || c.Gid != want[1] || c.Groups == nil || len(c.Groups) != 0 || c.NoSetGroups {
			t.Fatalf("slot %d credential %+v: want uid %d, gid %d and the supplementary groups dropped", i+1, c, want[0], want[1])
		}
		if !cmd.SysProcAttr.Setpgid {
			t.Fatal("lost its own process group, so KillTree would not reach the browsers")
		}
	}
}
