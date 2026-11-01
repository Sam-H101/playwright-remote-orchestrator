package runtime

import (
	"context"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"hash"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Source says where artefacts come from besides the embedded copy, and the hashes for versions that
// are not pinned in this build.
type Source struct {
	OfflineDir          string // folder holding the archives under their published names
	NodeMirror          string // default https://nodejs.org/dist
	NPMRegistry         string // default https://registry.npmjs.org
	NodeSHA256          string // required for an unpinned Node version
	PlaywrightIntegrity string // required for an unpinned playwright-core version
}

// Verifier checks a stream against a SHA-256 hex digest or an SRI integrity string.
type Verifier struct {
	h    hash.Hash
	want string
	kind string
}

// NewSHA256 verifies a hex SHA-256.
func NewSHA256(hexsum string) *Verifier {
	return &Verifier{h: sha256.New(), want: strings.ToLower(hexsum), kind: "sha256"}
}

// NewSRI verifies an SRI integrity string ("sha512-<base64>").
func NewSRI(sri string) (*Verifier, error) {
	algo, b64, ok := strings.Cut(sri, "-")
	if !ok {
		return nil, fmt.Errorf("bad integrity %q", sri)
	}
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return nil, fmt.Errorf("bad integrity %q: %v", sri, err)
	}
	switch algo {
	case "sha512":
		return &Verifier{h: sha512.New(), want: hex.EncodeToString(raw), kind: sri}, nil
	case "sha256":
		return &Verifier{h: sha256.New(), want: hex.EncodeToString(raw), kind: sri}, nil
	}
	return nil, fmt.Errorf("unsupported integrity algorithm %q", algo)
}

func (v *Verifier) Write(p []byte) (int, error) { return v.h.Write(p) }

// Fresh resets the digest so the verifier can check another copy.
func (v *Verifier) Fresh() *Verifier { v.h.Reset(); return v }

// Check compares the digest of everything written with the expected one.
func (v *Verifier) Check(name string) error {
	got := hex.EncodeToString(v.h.Sum(nil))
	if got != v.want {
		return fmt.Errorf("%s does not match its pinned hash (got %s %s, want %s)", name, v.kind, got, v.want)
	}
	return nil
}

// VerifyBytes checks in-memory content (the embedded archives).
func VerifyBytes(b []byte, v *Verifier, name string) error {
	v.Write(b)
	return v.Check(name)
}

// VerifyFile checks a file on disk.
func VerifyFile(path string, v *Verifier) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := io.Copy(v, f); err != nil {
		return err
	}
	return v.Check(filepath.Base(path))
}

// Download fetches url to dest, hashing while it streams into dest.part; the part file is renamed
// only when the hash matches and deleted otherwise. Proxies come from HTTPS_PROXY/HTTP_PROXY/NO_PROXY.
func Download(ctx context.Context, url, dest string, v *Verifier) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment}}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("download %s: %w", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download %s: HTTP %d", url, resp.StatusCode)
	}
	part := dest + ".part"
	f, err := os.Create(part)
	if err != nil {
		return err
	}
	_, err = io.Copy(io.MultiWriter(f, v), resp.Body)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = v.Check(filepath.Base(dest))
	}
	if err != nil {
		os.Remove(part)
		return fmt.Errorf("download %s: %w", url, err)
	}
	return os.Rename(part, dest)
}

// obtain puts a verified copy of an artefact at <cacheDir>/<file>: an existing verified cache entry,
// the embedded bytes, the offline folder, or a download, in that order.
func obtain(ctx context.Context, cacheDir, file, url string, embedded []byte, newV func() (*Verifier, error), offlineDir string) (string, error) {
	dest := filepath.Join(cacheDir, file)
	if _, err := os.Stat(dest); err == nil {
		v, err := newV()
		if err != nil {
			return "", err
		}
		if VerifyFile(dest, v) == nil {
			return dest, nil
		}
		os.Remove(dest)
	}
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		return "", err
	}
	if embedded != nil {
		v, err := newV()
		if err != nil {
			return "", err
		}
		if err := VerifyBytes(embedded, v, file+" (embedded)"); err != nil {
			return "", err
		}
		if err := writeAtomic(dest, embedded); err != nil {
			return "", err
		}
		return dest, nil
	}
	if offlineDir != "" {
		src := filepath.Join(offlineDir, file)
		if _, err := os.Stat(src); err == nil {
			v, err := newV()
			if err != nil {
				return "", err
			}
			if err := VerifyFile(src, v); err != nil {
				return "", err
			}
			if err := copyFile(src, dest); err != nil {
				return "", err
			}
			return dest, nil
		}
	}
	v, err := newV()
	if err != nil {
		return "", err
	}
	if err := Download(ctx, url, dest, v); err != nil {
		return "", err
	}
	return dest, nil
}

// FetchNode returns a verified Node archive in cacheDir.
func FetchNode(ctx context.Context, cacheDir, version, goos, goarch string, src Source) (string, error) {
	pin, err := NodePin(version, goos, goarch, src.NodeMirror, src.NodeSHA256)
	if err != nil {
		return "", err
	}
	var embedded []byte
	if nz, _, ok := Embedded(); ok && version == NodeVersion && goos == runtime.GOOS && goarch == runtime.GOARCH {
		embedded = nz
	}
	return obtain(ctx, cacheDir, pin.File(), pin.URL, embedded, func() (*Verifier, error) { return NewSHA256(pin.SHA256), nil }, src.OfflineDir)
}

// FetchPlaywright returns a verified playwright-core tarball in cacheDir.
func FetchPlaywright(ctx context.Context, cacheDir, version string, src Source) (string, error) {
	url, sri, err := PlaywrightPin(version, src.NPMRegistry, src.PlaywrightIntegrity)
	if err != nil {
		return "", err
	}
	var embedded []byte
	if _, pt, ok := Embedded(); ok && version == PlaywrightVersion && sri == PlaywrightPins[version] {
		embedded = pt
	}
	return obtain(ctx, cacheDir, PlaywrightFile(version), url, embedded, func() (*Verifier, error) { return NewSRI(sri) }, src.OfflineDir)
}

func writeAtomic(dest string, b []byte) error {
	tmp := dest + ".part"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, dest)
}

func copyFile(src, dest string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp := dest + ".part"
	out, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dest)
}
