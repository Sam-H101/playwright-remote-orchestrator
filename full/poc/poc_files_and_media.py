"""Files and media: moves real binary data through the grid in both directions and checks every byte.

Each round drives a small back office that the check serves itself (FakeSite), with data derived from the
round number, in a fresh context whose viewport and device scale factor change from round to round:
- downloads: CSV, UTF-8 named text and binary attachments (several MB in some rounds), saved with
  download.save_as and compared by size and SHA-256, plus a manifest CSV the page builds from a blob URL;
- uploads: in-memory files given to set_input_files or to a file chooser; the server hashes what arrived
  and the page shows that, which must match the check's own SHA-256 and size;
- screenshots: full-page, element, clip and JPEG viewport shots, whose sizes are read from the PNG IHDR
  and JPEG SOF headers and compared with the page, element and clip boxes (never pixel content);
- PDF: page.pdf() of a multi-page report, with the page count and orientation read from the file;
- tracing: a trace zip around the screenshot and PDF steps, which must hold that round's trace entries.
The first round also records a short video, which must be saved as a non-empty WebM file.
"""
import collections
import csv
import hashlib
import io
import random
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from playwright.sync_api import Browser, expect

from conftest import FakeSite, html

ORIGIN = "https://files.poc.test"
TIMEOUT_MS = 30 * 1000
# Several MB go through the websocket twice (check -> runner -> route handler), so allow time for that.
TRANSFER_TIMEOUT_MS = 90 * 1000
# Start another round only while at least this much of the budget is left.
RESERVE_S = 2.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EBML_SIGNATURE = b"\x1a\x45\xdf\xa3"  # WebM files are EBML documents
PDF_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
MEDIA_BOX = re.compile(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]")
PAPER_POINTS = {"A4": (595, 842), "Letter": (612, 792)}
CITIES = ["Zürich", "Kraków", "São Paulo", "東京", "Reykjavík", "Montréal", "Ålesund"]

UPLOAD_SCRIPT = """
const $ = (id) => document.getElementById(id);
const picker = $('picker');
$('choose').onclick = () => picker.click();
picker.onchange = () => { $('status').textContent = picker.files.length + ' selected'; };
$('send').onclick = async () => {
  $('rows').textContent = '';
  $('status').textContent = 'uploading';
  for (const f of picker.files) {
    // An ArrayBuffer body (not the File itself) keeps the bytes visible to request interception.
    const r = await fetch('/upload', {method: 'POST', body: await f.arrayBuffer(),
      headers: {'content-type': 'application/octet-stream', 'x-file-name': encodeURIComponent(f.name)}});
    const j = await r.json();
    const tr = document.createElement('tr');
    tr.dataset.name = j.name;
    for (const [cls, v] of [['name', j.name], ['client', f.size], ['size', j.size], ['sha', j.sha256]]) {
      const td = document.createElement('td');
      td.className = cls;
      td.textContent = v;
      tr.append(td);
    }
    $('rows').append(tr);
  }
  $('status').textContent = 'uploaded ' + picker.files.length;
};
$('export').onclick = () => {
  const lines = ['name,size,sha256'];
  for (const tr of document.querySelectorAll('#rows tr')) {
    lines.push([tr.dataset.name, tr.querySelector('.size').textContent, tr.querySelector('.sha').textContent].join(','));
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([lines.join('\\n') + '\\n'], {type: 'text/csv'}));
  a.download = 'manifest-r' + document.body.dataset.round + '.csv';
  document.body.append(a);
  a.click();
  a.remove();
};
"""

VIDEO_SCRIPT = """
window.framesDrawn = 0;
const box = document.getElementById('box');
(function tick() {
  window.framesDrawn++;
  box.style.transform = 'translateX(' + (window.framesDrawn * 3 % 240) + 'px)';
  requestAnimationFrame(tick);
})();
document.getElementById('step').onclick = () => {
  const c = document.getElementById('count');
  c.textContent = String(Number(c.textContent) + 1);
};
"""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def payload(seed, size):
    # A string seed makes random.Random reproducible across runs and processes.
    return random.Random(seed).randbytes(size)


# ---- round data -------------------------------------------------------------------------------------

def attachments(n):
    """What the server offers for download in round n: url path -> (label, filename, headers, body)."""
    rows = [["id", "sku", "city", "qty", "price"]]
    for i in range(1, 25 + n * 31):
        rows.append([i, "SKU-{:05d}".format(n * 1000 + i), CITIES[(n + i) % len(CITIES)], (i * 7 + n) % 50 + 1,
                     "{:.2f}".format((i * 13 + n * 5) % 9000 / 100 + 1)])
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    orders = buffer.getvalue().encode("utf-8")
    # Every third round moves a 3-6 MB binary; the others vary between about 40 KB and 1 MB.
    archive_size = 3_000_000 + (n * 211_111) % 3_000_000 if n % 3 == 0 else 40_000 + (n * 97_301) % 1_000_000
    archive = payload("archive-{}".format(n), archive_size)
    notes = "".join("Round {} note {}: shipment to {}\n".format(n, i, CITIES[i % len(CITIES)]) for i in range(n + 3)).encode()
    notes_name = "Notizen-Zürich-r{}.txt".format(n)
    return {
        "/files/orders-r{}.csv".format(n): ("Orders CSV", "orders-r{}.csv".format(n), {
            "content-type": "text/csv; charset=utf-8",
            "content-disposition": 'attachment; filename="orders-r{}.csv"'.format(n)}, orders),
        "/files/archive-r{}.bin".format(n): ("Archive", "archive-r{}.bin".format(n), {
            "content-type": "application/octet-stream",
            "content-disposition": 'attachment; filename="archive-r{}.bin"'.format(n)}, archive),
        # RFC 5987 filename*: the browser must prefer the UTF-8 name over the ASCII fallback.
        "/files/notes-r{}.txt".format(n): ("Notes", notes_name, {
            "content-type": "text/plain; charset=utf-8",
            "content-disposition": "attachment; filename=\"notes-r{}.txt\"; filename*=UTF-8''{}".format(
                n, quote(notes_name))}, notes),
    }, len(rows) - 1


def upload_files(n):
    """In-memory files to upload in round n, of varying size and kind."""
    files = [
        {"name": "note-r{}.txt".format(n), "mimeType": "text/plain",
         "buffer": "Round {} from {}\n".format(n, CITIES[n % len(CITIES)]).encode() * (n + 1)},
        {"name": "scan-r{}.bin".format(n), "mimeType": "application/octet-stream",
         "buffer": payload("scan-{}".format(n), 10_000 + (n * 49_973) % 600_000)},
    ]
    if n % 4 == 0:
        files.append({"name": "backup-r{}.bin".format(n), "mimeType": "application/octet-stream",
                      "buffer": payload("backup-{}".format(n), 2_000_000 + n * 7_919)})
    if n % 5 == 2:
        files.append({"name": "empty-r{}.dat".format(n), "mimeType": "application/octet-stream", "buffer": b""})
    return files


def layout(n):
    """Viewport, device scale factor and box sizes (CSS px) for round n's screenshots."""
    return {
        "viewport": {"width": 900 + (n % 4) * 60, "height": 640 + (n % 3) * 40},
        "scale": 2 if n % 2 == 0 else 1,
        "total": 1400 + (n % 5) * 230,
        "card": {"x": 40 + (n % 7) * 10, "y": 120 + (n % 4) * 25, "width": 220 + (n % 6) * 15, "height": 130 + (n % 5) * 10},
        "clip": {"x": 10 + (n % 3) * 5, "y": 20 + (n % 4) * 7, "width": 300 + (n % 5) * 20, "height": 180 + (n % 3) * 30},
        "pages": 2 + n % 4,
        "paper": "A4" if n % 2 else "Letter",
        "landscape": n % 3 == 0,
    }


# ---- the fake site ----------------------------------------------------------------------------------

def downloads_page(n, files):
    links = "".join("<li><a href='{}'>{}</a></li>".format(path, label) for path, (label, _, _, _) in files.items())
    return html("<h1>Exports for round {}</h1><ul>{}</ul>".format(n, links), "Exports")


def upload_page(n):
    body = ("<h1>Upload for round {}</h1>"
            "<input type='file' id='picker' multiple aria-label='Files'>"
            "<button id='choose'>Choose files</button> <button id='send'>Upload</button> "
            "<button id='export'>Export manifest</button>"
            "<p id='status' role='status'>idle</p>"
            "<table><thead><tr><th>Name</th><th>Client size</th><th>Server size</th><th>SHA-256</th></tr></thead>"
            "<tbody id='rows'></tbody></table><script>{}</script>").format(n, UPLOAD_SCRIPT)
    return html(body, "Upload").replace("<body>", "<body data-round='{}'>".format(n))


def gallery_page(n, plan):
    card = plan["card"]
    style = ("<style>html,body{{margin:0;padding:0;overflow-x:hidden}}"
             "#tall{{height:{}px;background:linear-gradient(#e8eef7,#20406a)}}"
             "#card{{position:absolute;box-sizing:border-box;left:{}px;top:{}px;width:{}px;height:{}px;"
             "background:#fff;border:3px solid #c33;overflow:hidden}}</style>").format(
                 plan["total"], card["x"], card["y"], card["width"], card["height"])
    return html("<div id='tall'></div><div id='card'><h2>Round {}</h2></div>".format(n), "Gallery", style)


def report_page(n, plan):
    count = plan["pages"]
    sections = "".join(
        "<section style='{}'><h2>Section {} of {}</h2><p>Round {} report for {}.</p></section>".format(
            "break-after:page" if i < count else "", i, count, n, CITIES[(n + i) % len(CITIES)])
        for i in range(1, count + 1))
    return html("<h1>Report {}</h1>{}".format(n, sections), "Report")


def video_page():
    body = ("<div id='box' style='width:60px;height:60px;background:#36c'></div>"
            "<button id='step'>Step</button> <span id='count'>0</span><script>{}</script>").format(VIDEO_SCRIPT)
    return html(body, "Video")


def make_handler(n, files, received):
    """The fake server for round n; `received` collects (name, bytes) of every upload it took."""
    def handler(route, request):
        url = urlsplit(request.url)
        if request.method == "POST" and url.path == "/upload":
            body = request.post_data_buffer or b""
            name = unquote(request.headers.get("x-file-name", ""))
            received.append((name, body))
            route.fulfill(json={"name": name, "size": len(body), "sha256": sha256(body)})
        elif url.path in files:
            _, _, headers, body = files[url.path]
            route.fulfill(status=200, headers=headers, body=body)
        elif url.path in ("/gallery", "/report"):
            plan = layout(int(parse_qs(url.query)["r"][0]))
            page = gallery_page if url.path == "/gallery" else report_page
            route.fulfill(content_type="text/html", body=page(n, plan))
        elif url.path == "/downloads":
            route.fulfill(content_type="text/html", body=downloads_page(n, files))
        elif url.path == "/upload":
            route.fulfill(content_type="text/html", body=upload_page(n))
        elif url.path == "/video":
            route.fulfill(content_type="text/html", body=video_page())
        else:
            route.fulfill(status=404, content_type="text/plain", body="not found")
    return handler


# ---- file format checks -----------------------------------------------------------------------------

def png_size(data, what):
    assert data[:8] == PNG_SIGNATURE and data[12:16] == b"IHDR", "{} is not a PNG (starts with {!r})".format(what, data[:16])
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def jpeg_size(data, what):
    assert data[:3] == b"\xff\xd8\xff", "{} is not a JPEG (starts with {!r})".format(what, data[:4])
    i = 2
    while i + 9 < len(data):
        assert data[i] == 0xFF, "{}: broken JPEG segment at byte {}".format(what, i)
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # start of frame: precision, height, width
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise AssertionError("{}: no JPEG start-of-frame segment".format(what))


def assert_size(actual, css_size, scale, what):
    expected = (round(css_size[0] * scale), round(css_size[1] * scale))
    assert actual == expected, "{} is {}x{} px, expected {}x{} ({}x{} CSS px at scale {})".format(
        what, actual[0], actual[1], expected[0], expected[1], css_size[0], css_size[1], scale)


# ---- round steps ------------------------------------------------------------------------------------

def check_screenshots(page, n, plan, work, totals):
    scale = plan["scale"]
    viewport = plan["viewport"]
    page.goto("{}/gallery?r={}".format(ORIGIN, n))
    card = page.locator("#card")
    expect(card).to_contain_text("Round {}".format(n))

    # Playwright sizes a full-page shot by the document's scroll size, as measured here.
    content_width = page.evaluate("""() => { const b = document.body, d = document.documentElement;
        return Math.max(b.scrollWidth, d.scrollWidth, b.offsetWidth, d.offsetWidth, b.clientWidth, d.clientWidth); }""")
    assert content_width <= viewport["width"], "gallery is {} CSS px wide, wider than the {} px viewport".format(
        content_width, viewport["width"])
    full = page.screenshot(full_page=True, path=str(work / "full.png"))
    assert (work / "full.png").read_bytes() == full, "full-page screenshot file differs from the returned bytes"
    assert_size(png_size(full, "full-page screenshot"), (content_width, plan["total"]), scale, "full-page screenshot")

    box = card.bounding_box()
    assert box, "the card has no bounding box"
    css_box = (box["width"], box["height"])
    assert css_box == (plan["card"]["width"], plan["card"]["height"]), "card box is {}, expected {}x{}".format(
        css_box, plan["card"]["width"], plan["card"]["height"])
    assert_size(png_size(card.screenshot(), "element screenshot"), css_box, scale, "element screenshot")
    # scale='css' must give one image pixel per CSS pixel whatever the device scale factor.
    assert_size(png_size(card.screenshot(scale="css"), "element screenshot (css)"), css_box, 1, "element screenshot (css)")

    clip = plan["clip"]
    shot = page.screenshot(clip=clip)
    assert_size(png_size(shot, "clip screenshot"), (clip["width"], clip["height"]), scale, "clip screenshot")

    jpeg = page.screenshot(type="jpeg", quality=40 + n % 50)
    assert jpeg.endswith(b"\xff\xd9"), "JPEG screenshot does not end with an end-of-image marker"
    assert_size(jpeg_size(jpeg, "JPEG screenshot"), (viewport["width"], viewport["height"]), scale, "JPEG screenshot")
    totals["screenshots"] += 5


def check_pdf(page, n, plan, work, totals):
    page.goto("{}/report?r={}".format(ORIGIN, n))
    expect(page.get_by_role("heading", name="Section {0} of {0}".format(plan["pages"]))).to_be_attached()
    target = work / "report.pdf"
    data = page.pdf(path=str(target), format=plan["paper"], landscape=plan["landscape"], print_background=True)
    assert target.read_bytes() == data, "saved PDF differs from the returned bytes"
    assert data.startswith(b"%PDF-"), "page.pdf() did not return a PDF (starts with {!r})".format(data[:8])
    assert b"%%EOF" in data[-1024:], "PDF has no %%EOF trailer"
    pages = len(PDF_PAGE.findall(data))
    assert pages == plan["pages"], "PDF has {} page objects, expected {}".format(pages, plan["pages"])
    match = MEDIA_BOX.search(data)
    assert match, "PDF has no /MediaBox"
    width, height = float(match.group(3)) - float(match.group(1)), float(match.group(4)) - float(match.group(2))
    paper = sorted(PAPER_POINTS[plan["paper"]], reverse=plan["landscape"])
    assert abs(width - paper[0]) < 3 and abs(height - paper[1]) < 3, "PDF page is {:.0f}x{:.0f} pt, expected {} {} ({}x{})".format(
        width, height, plan["paper"], "landscape" if plan["landscape"] else "portrait", paper[0], paper[1])
    totals["pdf_pages"] += pages


def check_trace(path, n, totals):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        traces = [name for name in names if name.endswith(".trace")]
        assert traces, "trace zip has no .trace entry (entries: {})".format(names[:20])
        assert any(name.endswith(".network") for name in names), "trace zip has no .network entry"
        events = b"".join(archive.read(name) for name in traces)
    lines = [line for line in events.splitlines() if line.strip()]
    assert len(lines) >= 10, "trace holds only {} events".format(len(lines))
    marker = "files.poc.test/gallery?r={}".format(n).encode()
    assert marker in events, "trace does not mention round {}'s gallery navigation".format(n)
    totals["traces"] += 1
    totals["trace_events"] += len(lines)


def check_uploads(page, n, received, totals):
    files = upload_files(n)
    page.goto("{}/upload".format(ORIGIN))
    if n % 2:
        page.get_by_label("Files").set_input_files(files)
    else:
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Choose files").click()
        assert chooser.value.is_multiple(), "the file chooser does not accept several files"
        chooser.value.set_files(files)
    status = page.get_by_role("status")
    expect(status).to_have_text("{} selected".format(len(files)))
    page.get_by_role("button", name="Upload").click()
    expect(status).to_have_text("uploaded {}".format(len(files)), timeout=TRANSFER_TIMEOUT_MS)

    for f in files:
        row = page.locator("#rows tr[data-name='{}']".format(f["name"]))
        size, digest = len(f["buffer"]), sha256(f["buffer"])
        expect(row.locator(".client"), "browser size of {}".format(f["name"])).to_have_text(str(size))
        expect(row.locator(".size"), "server size of {}".format(f["name"])).to_have_text(str(size))
        expect(row.locator(".sha"), "server SHA-256 of {}".format(f["name"])).to_have_text(digest)
        totals["bytes_up"] += size
    assert [name for name, _ in received] == [f["name"] for f in files], "server got uploads {}, expected {}".format(
        [name for name, _ in received], [f["name"] for f in files])
    totals["uploads"] += len(files)
    return files


def check_manifest(page, n, files, work, totals):
    """The page builds a CSV of what the server reported and downloads it from a blob URL."""
    with page.expect_download() as info:
        page.get_by_role("button", name="Export manifest").click()
    download = info.value
    expected_name = "manifest-r{}.csv".format(n)
    assert download.suggested_filename == expected_name, "manifest saved as {!r}, expected {!r}".format(
        download.suggested_filename, expected_name)
    target = work / expected_name
    download.save_as(str(target))
    expected = "name,size,sha256\n" + "".join(
        "{},{},{}\n".format(f["name"], len(f["buffer"]), sha256(f["buffer"])) for f in files)
    assert target.read_text(encoding="utf-8") == expected, "manifest differs:\n{}".format(target.read_text(encoding="utf-8"))
    totals["downloads"] += 1


def check_downloads(page, n, files, csv_rows, work, totals):
    page.goto("{}/downloads".format(ORIGIN))
    expect(page.get_by_role("heading", name="Exports for round {}".format(n))).to_be_visible()
    for path, (label, filename, _, body) in files.items():
        with page.expect_download(timeout=TRANSFER_TIMEOUT_MS) as info:
            page.get_by_role("link", name=label, exact=True).click()
        download = info.value
        assert download.url == ORIGIN + path, "{} came from {}, expected {}".format(label, download.url, ORIGIN + path)
        assert download.suggested_filename == filename, "{} suggested {!r}, expected {!r}".format(
            label, download.suggested_filename, filename)
        target = work / "dl" / filename
        download.save_as(str(target))
        assert download.failure() is None, "{} failed: {}".format(label, download.failure())
        data = target.read_bytes()
        assert len(data) == len(body), "{} saved {} bytes, expected {}".format(label, len(data), len(body))
        assert sha256(data) == sha256(body), "{} saved with SHA-256 {}, expected {}".format(label, sha256(data), sha256(body))
        if filename.endswith(".csv"):
            rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
            assert len(rows) - 1 == csv_rows, "{} has {} data rows, expected {}".format(label, len(rows) - 1, csv_rows)
        totals["downloads"] += 1
        totals["bytes_down"] += len(data)
        totals["largest_download"] = max(totals["largest_download"], len(data))


def record_video(browser: Browser, work, totals):
    files, _ = attachments(0)
    context = browser.new_context(viewport={"width": 640, "height": 480}, record_video_dir=str(work / "video"),
                                  record_video_size={"width": 320, "height": 240})
    try:
        FakeSite(context, ORIGIN, make_handler(0, files, []))
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        page.goto("{}/video".format(ORIGIN))
        for i in range(1, 4):
            page.get_by_role("button", name="Step").click()
            expect(page.locator("#count")).to_have_text(str(i))
        # The recording needs frames, so let the page animate for about a second's worth of them.
        page.wait_for_function("window.framesDrawn >= 60")
    finally:
        context.close()
    target = work / "clip.webm"
    # The video lives on the runner; save_as waits for it to be finalized and copies it over.
    page.video.save_as(str(target))
    data = target.read_bytes()
    assert len(data) > 1000, "recorded video is only {} bytes".format(len(data))
    assert data[:4] == EBML_SIGNATURE, "recorded video is not WebM (starts with {!r})".format(data[:4])
    totals["video_bytes"] = len(data)


def run_round(browser: Browser, capture, n, work, totals):
    plan = layout(n)
    files, csv_rows = attachments(n)
    received = []
    context = browser.new_context(viewport=plan["viewport"], device_scale_factor=plan["scale"], accept_downloads=True)
    site = FakeSite(context, ORIGIN, make_handler(n, files, received))
    page = capture.add(context.new_page())
    page.set_default_timeout(TIMEOUT_MS)

    context.tracing.start(name="round-{}".format(n), screenshots=True, snapshots=True)
    check_screenshots(page, n, plan, work, totals)
    check_pdf(page, n, plan, work, totals)
    context.tracing.stop(path=str(work / "trace.zip"))
    check_trace(work / "trace.zip", n, totals)

    uploaded = check_uploads(page, n, received, totals)
    check_manifest(page, n, uploaded, work, totals)
    check_downloads(page, n, files, csv_rows, work, totals)
    assert site.count("POST", "/upload") == len(uploaded), "server saw {} upload requests, expected {}".format(
        site.count("POST", "/upload"), len(uploaded))
    # Closed only after a passing round: on failure the page stays open for capture's screenshot and the
    # grid_browser fixture closes everything.
    context.close()


def test_files_and_media(grid_browser: Browser, budget, capture):
    totals = collections.Counter()
    slowest = 0.0
    with tempfile.TemporaryDirectory(prefix="poc-files-") as tmp:
        while budget.more(reserve_s=RESERVE_S):
            n = budget.rounds
            started = time.monotonic()
            work = Path(tmp) / "round-{}".format(n)
            work.mkdir()
            if n == 1:
                record_video(grid_browser, work, totals)
            run_round(grid_browser, capture, n, work, totals)
            shutil.rmtree(work, ignore_errors=True)
            slowest = max(slowest, time.monotonic() - started)
    print("files and media: {} rounds in {:.0f}s (slowest {:.1f}s): {} downloads ({:.1f} MB, largest {:.1f} MB), "
          "{} uploads ({:.1f} MB), {} screenshots, {} PDF pages, {} traces ({} events), video {} bytes".format(
              budget.rounds, budget.elapsed(), slowest, totals["downloads"], totals["bytes_down"] / 1e6,
              totals["largest_download"] / 1e6, totals["uploads"], totals["bytes_up"] / 1e6, totals["screenshots"],
              totals["pdf_pages"], totals["traces"], totals["trace_events"], totals["video_bytes"]))
