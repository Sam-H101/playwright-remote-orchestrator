package runtime

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// safeRel cleans an archive entry name and rejects absolute paths and anything that climbs out ("..").
func safeRel(name string) (string, error) {
	name = strings.ReplaceAll(name, `\`, "/")
	if strings.HasPrefix(name, "/") || (len(name) > 1 && name[1] == ':') {
		return "", fmt.Errorf("archive entry %q has an absolute path", name)
	}
	clean := path.Clean(name)
	if clean == ".." || strings.HasPrefix(clean, "../") || strings.Contains(name, "/../") || strings.HasSuffix(name, "/..") {
		return "", fmt.Errorf("archive entry %q leaves the destination", name)
	}
	return clean, nil
}

// ExtractNode writes only the Node binary and its LICENSE from a release archive into destDir: from a
// zip "node-v*/node.exe" and "node-v*/LICENSE", from a tar.gz "node-v*/bin/node" and "node-v*/LICENSE".
func ExtractNode(archive, destDir string) error {
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return err
	}
	found := map[string]bool{}
	if strings.HasSuffix(archive, ".zip") {
		zr, err := zip.OpenReader(archive)
		if err != nil {
			return err
		}
		defer zr.Close()
		for _, f := range zr.File {
			rel, err := safeRel(f.Name)
			if err != nil {
				return err
			}
			parts := strings.Split(rel, "/")
			if len(parts) != 2 || !strings.HasPrefix(parts[0], "node-v") || (parts[1] != "node.exe" && parts[1] != "LICENSE") {
				continue
			}
			if f.Mode()&os.ModeSymlink != 0 || !f.Mode().IsRegular() {
				return fmt.Errorf("archive entry %q is not a regular file", f.Name)
			}
			rc, err := f.Open()
			if err != nil {
				return err
			}
			err = writeFile(filepath.Join(destDir, parts[1]), rc, 0o755)
			rc.Close()
			if err != nil {
				return err
			}
			found[parts[1]] = true
		}
		if !found["node.exe"] {
			return fmt.Errorf("%s has no node-v*/node.exe", filepath.Base(archive))
		}
		return nil
	}
	err := walkTarGz(archive, func(rel string, h *tar.Header, r io.Reader) error {
		parts := strings.Split(rel, "/")
		var name string
		switch {
		case len(parts) == 3 && parts[1] == "bin" && parts[2] == "node":
			name = "node"
		case len(parts) == 2 && parts[1] == "LICENSE":
			name = "LICENSE"
		default:
			return nil
		}
		if !strings.HasPrefix(parts[0], "node-v") {
			return nil
		}
		if h.Typeflag != tar.TypeReg {
			return fmt.Errorf("archive entry %q is not a regular file", h.Name)
		}
		found[name] = true
		return writeFile(filepath.Join(destDir, name), r, 0o755)
	})
	if err == nil && !found["node"] {
		err = fmt.Errorf("%s has no node-v*/bin/node", filepath.Base(archive))
	}
	return err
}

// ExtractPlaywright writes the "package/" tree of an npm tarball into destDir. Links and anything that
// would land outside destDir are refused.
func ExtractPlaywright(tgz, destDir string) error {
	n := 0
	err := walkTarGz(tgz, func(rel string, h *tar.Header, r io.Reader) error {
		rest, ok := strings.CutPrefix(rel, "package/")
		if !ok || rest == "" {
			return nil
		}
		switch h.Typeflag {
		case tar.TypeDir:
			return os.MkdirAll(filepath.Join(destDir, filepath.FromSlash(rest)), 0o755)
		case tar.TypeReg:
			n++
			return writeFile(filepath.Join(destDir, filepath.FromSlash(rest)), r, 0o644|os.FileMode(h.Mode)&0o111)
		default:
			return fmt.Errorf("archive entry %q is not a regular file or folder", h.Name)
		}
	})
	if err == nil && n == 0 {
		err = fmt.Errorf("%s has no package/ files", filepath.Base(tgz))
	}
	return err
}

func walkTarGz(archive string, fn func(rel string, h *tar.Header, r io.Reader) error) error {
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if h.Typeflag == tar.TypeXGlobalHeader || h.Typeflag == tar.TypeXHeader {
			continue
		}
		rel, err := safeRel(h.Name)
		if err != nil {
			return err
		}
		if err := fn(rel, h, tr); err != nil {
			return err
		}
	}
}

func writeFile(dest string, r io.Reader, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	out, err := os.OpenFile(dest, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, r); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}
