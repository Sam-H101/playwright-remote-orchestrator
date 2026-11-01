package runtime

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"os"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"testing"

	"github.com/Sam-H101/playwright-remote-orchestrator/runner/internal/logging"
)

type entry struct {
	name, body string
	link       bool
}

func makeZip(t *testing.T, path string, entries []entry) {
	f, _ := os.Create(path)
	zw := zip.NewWriter(f)
	for _, e := range entries {
		h := &zip.FileHeader{Name: e.name, Method: zip.Deflate}
		if e.link {
			h.SetMode(os.ModeSymlink | 0o777)
		} else {
			h.SetMode(0o644)
		}
		w, err := zw.CreateHeader(h)
		if err != nil {
			t.Fatal(err)
		}
		w.Write([]byte(e.body))
	}
	zw.Close()
	f.Close()
}

func makeTgz(t *testing.T, path string, entries []entry) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, e := range entries {
		h := &tar.Header{Name: e.name, Mode: 0o644, Size: int64(len(e.body)), Typeflag: tar.TypeReg}
		if e.link {
			h = &tar.Header{Name: e.name, Linkname: "../../etc/passwd", Typeflag: tar.TypeSymlink}
		}
		if err := tw.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		tw.Write([]byte(e.body))
	}
	tw.Close()
	gz.Close()
	os.WriteFile(path, buf.Bytes(), 0o644)
}

func TestExtractNodeKeepsOnlyTheBinary(t *testing.T) {
	dir := t.TempDir()
	z := filepath.Join(dir, "node.zip")
	makeZip(t, z, []entry{{name: "node-v1/node.exe", body: "exe"}, {name: "node-v1/LICENSE", body: "lic"}, {name: "node-v1/npm.cmd", body: "x"},
		{name: "node-v1/node_modules/npm/LICENSE", body: "y"}})
	out := filepath.Join(dir, "out")
	if err := ExtractNode(z, out); err != nil {
		t.Fatal(err)
	}
	names, _ := os.ReadDir(out)
	if len(names) != 2 {
		t.Fatalf("extracted %v", names)
	}
	if b, _ := os.ReadFile(filepath.Join(out, "node.exe")); string(b) != "exe" {
		t.Fatal(string(b))
	}
	tgz := filepath.Join(dir, "node.tar.gz")
	makeTgz(t, tgz, []entry{{name: "node-v1/bin/node", body: "elf"}, {name: "node-v1/LICENSE", body: "lic"}, {name: "node-v1/bin/npm", body: "x"}})
	if err := ExtractNode(tgz, filepath.Join(dir, "out2")); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "out2", "node")); string(b) != "elf" {
		t.Fatal(string(b))
	}
}

func TestExtractRejectsEscapesAndLinks(t *testing.T) {
	dir := t.TempDir()
	for name, entries := range map[string][]entry{
		"dotdot":   {{name: "package/../../evil.js", body: "x"}},
		"absolute": {{name: "/package/evil.js", body: "x"}},
		"link":     {{name: "package/link", link: true}},
	} {
		tgz := filepath.Join(dir, name+".tgz")
		makeTgz(t, tgz, entries)
		if err := ExtractPlaywright(tgz, filepath.Join(dir, "out-"+name)); err == nil {
			t.Fatalf("%s: extraction must fail", name)
		}
	}
	if _, err := os.Stat(filepath.Join(dir, "evil.js")); err == nil {
		t.Fatal("zip-slip wrote outside the destination")
	}
	z := filepath.Join(dir, "slip.zip")
	makeZip(t, z, []entry{{name: "../node-v1/node.exe", body: "x"}})
	if err := ExtractNode(z, filepath.Join(dir, "zout")); err == nil {
		t.Fatal("zip entry with .. must fail")
	}
	z = filepath.Join(dir, "link.zip")
	makeZip(t, z, []entry{{name: "node-v1/node.exe", link: true}})
	if err := ExtractNode(z, filepath.Join(dir, "lout")); err == nil {
		t.Fatal("symlinked node.exe must fail")
	}
}

func TestVerifiers(t *testing.T) {
	data := []byte("hello")
	sum := sha256.Sum256(data)
	if err := VerifyBytes(data, NewSHA256(hex.EncodeToString(sum[:])), "x"); err != nil {
		t.Fatal(err)
	}
	if err := VerifyBytes(data, NewSHA256(strings.Repeat("0", 64)), "x"); err == nil {
		t.Fatal("mismatch must fail")
	}
	s512 := sha512.Sum512(data)
	v, err := NewSRI("sha512-" + base64.StdEncoding.EncodeToString(s512[:]))
	if err != nil || VerifyBytes(data, v, "x") != nil {
		t.Fatal("sri", err)
	}
	if _, err := NewSRI("md5-abc"); err == nil {
		t.Fatal("unsupported algorithm")
	}
}

func TestPinsRequireHashesForUnpinnedVersions(t *testing.T) {
	if _, err := NodePin("1.2.3", "windows", "amd64", "", ""); err == nil || !strings.Contains(err.Error(), "--node-sha256") {
		t.Fatal(err)
	}
	if _, _, err := PlaywrightPin("0.0.0-does-not-exist", "", ""); err == nil || !strings.Contains(err.Error(), "--playwright-integrity") {
		t.Fatal(err)
	}
	p, err := NodePin(NodeVersion, "windows", "amd64", "https://mirror.example/node/", "")
	if err != nil || p.URL != "https://mirror.example/node/v22.23.4/node-v22.23.4-win-x64.zip" || p.SHA256 != NodePins["windows/amd64"].SHA256 {
		t.Fatal(p, err)
	}
}

// offline builds a synthetic Node archive and playwright-core tarball in an offline folder and returns
// the Source and wanted Manifest for them.
func offline(t *testing.T, version, pwVersion, marker string) (Source, Manifest) {
	dir := t.TempDir()
	file, _ := NodeArchive(version, goruntime.GOOS, goruntime.GOARCH)
	archive := filepath.Join(dir, file)
	bin := "node.exe"
	if goruntime.GOOS != "windows" {
		bin = "bin/node"
	}
	if strings.HasSuffix(file, ".zip") {
		makeZip(t, archive, []entry{{name: "node-v" + version + "/" + bin, body: marker}, {name: "node-v" + version + "/LICENSE", body: "lic"}})
	} else {
		makeTgz(t, archive, []entry{{name: "node-v" + version + "/" + bin, body: marker}, {name: "node-v" + version + "/LICENSE", body: "lic"}})
	}
	tgz := filepath.Join(dir, PlaywrightFile(pwVersion))
	makeTgz(t, tgz, []entry{{name: "package/cli.js", body: "// " + marker}, {name: "package/package.json", body: `{"version":"` + pwVersion + `"}`}})
	nb, _ := os.ReadFile(archive)
	ns := sha256.Sum256(nb)
	pb, _ := os.ReadFile(tgz)
	ps := sha512.Sum512(pb)
	src := Source{OfflineDir: dir, NodeSHA256: hex.EncodeToString(ns[:]), PlaywrightIntegrity: "sha512-" + base64.StdEncoding.EncodeToString(ps[:])}
	want, err := Want(version, pwVersion, "test", src)
	if err != nil {
		t.Fatal(err)
	}
	return src, want
}

func TestLayDownSkipSwapAndRollback(t *testing.T) {
	nodeVersionOf = func(_ context.Context, node string) (string, error) {
		b, _ := os.ReadFile(node)
		_, v, _ := strings.Cut(string(b), "@")
		return "v" + v, nil
	}
	defer func() { nodeVersionOf = NodeVersionOf }()
	var logBuf bytes.Buffer
	log := logging.New(&logBuf, nil)
	dir := t.TempDir()
	ctx := context.Background()

	src1, want1 := offline(t, "0.0.1", "0.0.1-pw", "one@0.0.1")
	changed, err := LayDown(ctx, dir, want1, src1, log)
	if err != nil || !changed {
		t.Fatal(changed, err)
	}
	if b, _ := os.ReadFile(CLI(dir)); string(b) != "// one@0.0.1" {
		t.Fatalf("cli.js %q", b)
	}
	if _, err := os.Stat(Watchdog(dir)); err != nil {
		t.Fatal("watchdog not laid down")
	}
	m, _, _ := ReadManifest(dir)
	if m.PlaywrightVersion != "0.0.1-pw" || m.Source != "offline" || m.Fetched == "" {
		t.Fatalf("%+v", m)
	}

	changed, err = LayDown(ctx, dir, want1, src1, log)
	if err != nil || changed || !strings.Contains(logBuf.String(), "already installed, skipping npm") {
		t.Fatalf("second lay-down: %v %v\n%s", changed, err, logBuf.String())
	}

	src2, want2 := offline(t, "0.0.2", "0.0.2-pw", "two@0.0.2")
	if changed, err = LayDown(ctx, dir, want2, src2, log); err != nil || !changed {
		t.Fatal(changed, err)
	}
	if b, _ := os.ReadFile(CLI(dir)); string(b) != "// two@0.0.2" {
		t.Fatalf("after upgrade cli.js %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(dir, "app.prev", "node_modules", "playwright-core", "cli.js")); string(b) != "// one@0.0.1" {
		t.Fatalf("app.prev %q", b)
	}
	if err := Rollback(dir); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(CLI(dir)); string(b) != "// one@0.0.1" {
		t.Fatalf("after rollback cli.js %q", b)
	}
	if m, _, _ := ReadManifest(dir); m.PlaywrightVersion != "0.0.1-pw" {
		t.Fatalf("manifest after rollback %+v", m)
	}
}

func TestHashMismatchFailsAndLeavesNothing(t *testing.T) {
	src, want := offline(t, "0.0.3", "0.0.3-pw", "x@0.0.3")
	want.PlaywrightIntegrity = "sha512-" + base64.StdEncoding.EncodeToString(make([]byte, 64))
	dir := t.TempDir()
	if _, err := LayDown(context.Background(), dir, want, src, logging.Discard()); err == nil || !strings.Contains(err.Error(), "does not match its pinned hash") {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "cache", PlaywrightFile("0.0.3-pw"))); err == nil {
		t.Fatal("a file that failed verification was kept")
	}
	if _, err := os.Stat(filepath.Join(dir, "app")); err == nil {
		t.Fatal("app laid down despite the failure")
	}
}
