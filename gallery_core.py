"""Gallery -> PDF/CBZ and video downloader core (Windows/desktop port of the iOS app)."""
import html as _html
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import uuid
import zlib

import requests

IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
LETTER_EXT = {"j": "jpg", "p": "png", "g": "gif", "w": "webp", "a": "avif"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


class JobError(Exception):
    pass


class Cancelled(Exception):
    pass


class LimitReached(JobError):
    pass


class Reporter:
    """Callbacks used by the engines. The GUI replaces these."""

    def __init__(self, log=None, progress=None, cancel_event=None):
        self._log = log or (lambda s: print(s))
        self._progress = progress or (lambda cur, total, text: None)
        self.cancel = cancel_event or threading.Event()

    def log(self, s):
        self._log(str(s))

    def progress(self, cur, total, text=""):
        self._progress(cur, total, text)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()


# ---------------------------------------------------------------- helpers
def safe_name(n):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n or "").strip()
    s = s.rstrip(". ")
    if len(s) > 120:
        s = s[:120].rstrip(". ")
    if s.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)\d", s.upper() or ""):
        s = "_" + s
    return s or "gallery"


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_images(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted((n for n in names if n.rsplit(".", 1)[-1].lower() in IMG_EXT and not n.endswith(".part")),
                  key=natural_key)


def already_have(folder, index):
    prefix = "%04d." % index
    try:
        return any(n.startswith(prefix) and not n.endswith(".part") for n in os.listdir(folder))
    except OSError:
        return False


def steps_for(total, mode):
    steps = max(total, 1)
    if mode in ("pdf", "both"):
        steps += total + 1
    if mode in ("cbz", "both"):
        steps += 1
    return steps


def cookie_pairs(raw):
    out = []
    for part in re.split(r"[;,\n\r]", raw or ""):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out.append((k, v))
    return out


def unescape(s):
    return _html.unescape(s)


def sleep(cb, seconds):
    end = time.time() + max(seconds, 0)
    while time.time() < end:
        cb.check()
        time.sleep(min(0.1, max(end - time.time(), 0)))
    cb.check()


# ---------------------------------------------------------------- network
class Fetcher:
    def __init__(self, cb):
        self.cb = cb
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.extra = {}

    def set_cookie(self, name, value, domain):
        self.s.cookies.set(name, value, domain=domain, path="/")

    def _headers(self, referer):
        h = dict(self.extra)
        if referer:
            h["Referer"] = referer
        return h

    def _get(self, url, referer=None, retries=3, stream=False, timeout=30):
        last = None
        for attempt in range(retries):
            self.cb.check()
            try:
                r = self.s.get(url, headers=self._headers(referer), timeout=timeout, stream=stream)
                if r.status_code >= 400:
                    code = r.status_code
                    r.close()
                    if code in (403, 404, 410) and attempt >= 1:
                        raise JobError("HTTP %d" % code)
                    last = JobError("HTTP %d" % code)
                else:
                    return r
            except JobError as e:
                last = e
                if "404" in str(e) or "410" in str(e) or "403" in str(e):
                    break
            except requests.RequestException as e:
                last = JobError(str(e))
            if attempt < retries - 1:
                sleep(self.cb, 1.5 * (attempt + 1))
        raise last or JobError("σφάλμα δικτύου")

    def text(self, url, referer=None, retries=3):
        r = self._get(url, referer, retries)
        r.encoding = r.encoding or "utf-8"
        return r.text

    def data(self, url, referer=None, retries=3):
        return self._get(url, referer, retries).content

    def download(self, url, dst, referer=None, retries=3, progress=None):
        part = dst + ".part"
        r = self._get(url, referer, retries, stream=True, timeout=60)
        try:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    self.cb.check()
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
        except requests.RequestException as e:
            raise JobError(str(e))
        finally:
            r.close()
        if os.path.exists(dst):
            os.remove(dst)
        os.replace(part, dst)


# ---------------------------------------------------------------- output (PDF / CBZ)
def jpeg_info(path):
    try:
        with open(path, "rb") as f:
            b = f.read(1 << 20)
    except OSError:
        return None
    if len(b) < 4 or b[0] != 0xFF or b[1] != 0xD8:
        return None
    p = 2
    while p + 4 < len(b):
        if b[p] != 0xFF:
            p += 1
            continue
        while p < len(b) and b[p] == 0xFF:
            p += 1
        if p >= len(b):
            return None
        m = b[p]
        p += 1
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            continue
        if p + 2 > len(b):
            return None
        ln = (b[p] << 8) | b[p + 1]
        if m in (0xC0, 0xC1, 0xC2):
            if p + 8 >= len(b):
                return None
            h = (b[p + 3] << 8) | b[p + 4]
            w = (b[p + 5] << 8) | b[p + 6]
            return w, h, b[p + 7]
        if m == 0xDA:
            return None
        p += ln
    return None


def prepare_images(folder, tmp, cb, step):
    from PIL import Image
    files = list_images(folder)
    pages = []
    for i, name in enumerate(files):
        cb.check()
        src = os.path.join(folder, name)
        ext = name.rsplit(".", 1)[-1].lower()
        info = jpeg_info(src) if ext in ("jpg", "jpeg") else None
        if info and info[2] in (1, 3):
            pages.append((src, info[0], info[1], info[2] == 1))
        else:
            dst = os.path.join(tmp, os.path.splitext(name)[0] + ".jpg")
            try:
                with Image.open(src) as im:
                    im.load()
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGBA")
                        bg = Image.new("RGB", im.size, (255, 255, 255))
                        bg.paste(im, mask=im.split()[-1])
                        im = bg
                    elif im.mode != "RGB":
                        im = im.convert("RGB")
                    im.save(dst, "JPEG", quality=95)
                    pages.append((dst, im.width, im.height, False))
            except Exception as e:  # noqa: BLE001
                cb.log("Παράλειψη %s: %s" % (name, e))
        step(i + 1, len(files))
    return pages


def build_pdf(pages, path, title):
    n = len(pages)
    offsets = {}
    with open(path, "wb") as f:
        def obj(num, body):
            offsets[num] = f.tell()
            f.write(("%d 0 obj\n" % num).encode() + body + b"\nendobj\n")

        f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join("%d 0 R" % (4 + 3 * i) for i in range(n))
        obj(2, ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n)).encode())
        if title:
            hexs = "FEFF" + title.encode("utf-16-be").hex().upper()
            obj(3, ("<< /Title <%s> /Producer (GalleryToPDF) >>" % hexs).encode())
        else:
            obj(3, b"<< /Producer (GalleryToPDF) >>")
        for i, (p, w, h, gray) in enumerate(pages):
            page_n, img_n, cont_n = 4 + 3 * i, 5 + 3 * i, 6 + 3 * i
            pw, ph = w * 0.75, h * 0.75
            obj(page_n, ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                         % (pw, ph, img_n, cont_n)).encode())
            with open(p, "rb") as im:
                data = im.read()
            cs = "/DeviceGray" if gray else "/DeviceRGB"
            offsets[img_n] = f.tell()
            f.write(("%d 0 obj\n<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
                     % (img_n, w, h, cs, len(data))).encode())
            f.write(data)
            f.write(b"\nendstream\nendobj\n")
            content = ("q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (pw, ph)).encode()
            obj(cont_n, ("<< /Length %d >>\nstream\n" % len(content)).encode() + content + b"\nendstream")
        total = 3 + 3 * n
        xref_pos = f.tell()
        f.write(("xref\n0 %d\n0000000000 65535 f \n" % (total + 1)).encode())
        for num in range(1, total + 1):
            f.write(("%010d 00000 n \n" % offsets.get(num, 0)).encode())
        f.write(("trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (total + 1, xref_pos)).encode())


def build_cbz(folder, path):
    import zipfile
    names = list_images(folder)
    part = path + ".part"
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as z:
        for n in names:
            z.write(os.path.join(folder, n), n)
    if os.path.exists(path):
        os.remove(path)
    os.replace(part, path)
    return len(names)


def finish_job(img_dir, name, title, out_dir, work_root, total, failed, mode, cb):
    count = len(list_images(img_dir))
    if count == 0:
        raise JobError("Δεν κατέβηκε καμία εικόνα.")
    steps = steps_for(total, mode)
    pos = max(total, 1)
    outputs = []
    conv = os.path.join(work_root, "conv_" + uuid.uuid4().hex)
    os.makedirs(conv, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    try:
        if mode in ("pdf", "both"):
            cb.log("== Προετοιμασία εικόνων ==")
            base = pos
            pages = prepare_images(img_dir, conv, cb, lambda n, t: cb.progress(base + n, steps, "Προετοιμασία %d/%d" % (n, t)))
            if not pages:
                raise JobError("Δεν κατέβηκε καμία εικόνα.")
            pos += total
            cb.progress(pos, steps, "Δημιουργία PDF...")
            pdf = os.path.join(out_dir, name + ".pdf")
            build_pdf(pages, pdf, title)
            pos += 1
            outputs.append(pdf)
            cb.log("Έτοιμο PDF (%d σελίδες)" % len(pages))
        if mode in ("cbz", "both"):
            cb.progress(pos, steps, "Δημιουργία CBZ...")
            cbz = os.path.join(out_dir, name + ".cbz")
            n = build_cbz(img_dir, cbz)
            pos += 1
            outputs.append(cbz)
            cb.log("Έτοιμο CBZ (%d σελίδες)" % n)
    finally:
        shutil.rmtree(conv, ignore_errors=True)
    if failed:
        cb.log("Προσοχή: απέτυχαν οι σελίδες %s. Πάτα ξανά «Κατέβασε» με το ίδιο link: όσες κατέβηκαν δεν θα ξανακατέβουν." % failed)
    else:
        shutil.rmtree(img_dir, ignore_errors=True)
    cb.progress(steps, steps, "Έτοιμο ✔  (%d σελίδες)" % count)
    return outputs


# ---------------------------------------------------------------- shared download loop
def download_items(items, f, name, title, out_dir, work_root, referer, mode, delay, cb):
    """items: list of (index, [candidate urls])"""
    total = len(items)
    img_dir = os.path.join(work_root, name)
    os.makedirs(img_dir, exist_ok=True)
    steps = steps_for(total, mode)
    failed = []
    first_error = ""

    def fetch_one(idx, cands):
        last = JobError("δεν βρέθηκε η εικόνα")
        for url in cands:
            cb.check()
            ext = os.path.splitext(url.split("?")[0])[1].lstrip(".").lower()
            if ext not in IMG_EXT:
                ext = "jpg"
            dst = os.path.join(img_dir, "%04d.%s" % (idx, ext))
            try:
                f.download(url, dst, referer=referer, retries=2)
                return
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
        raise last

    cb.log("== Λήψη εικόνων ==")
    for idx, cands in items:
        cb.check()
        if already_have(img_dir, idx):
            cb.log("%d/%d: υπάρχει ήδη, το προσπερνάω" % (idx, total))
        else:
            try:
                fetch_one(idx, cands)
                cb.log("%d/%d: OK" % (idx, total))
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                failed.append(idx)
                first_error = first_error or str(e)
                cb.log("%d/%d: ΑΠΟΤΥΧΙΑ (%s)" % (idx, total, e))
            sleep(cb, delay)
        cb.progress(idx, steps, "Λήψη %d/%d" % (idx, total))

    if failed:
        cb.log("== Δεύτερη προσπάθεια για %d σελίδες ==" % len(failed))
        still = []
        for i in failed:
            sleep(cb, max(delay, 1.0) * 2)
            try:
                fetch_one(i, items[i - 1][1])
                cb.log("%d/%d: OK (2η προσπάθεια)" % (i, total))
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                still.append(i)
                cb.log("%d/%d: ξανά ΑΠΟΤΥΧΙΑ (%s)" % (i, total, e))
        failed = still

    if not list_images(img_dir):
        if "403" in first_error or "cloudflare" in first_error.lower():
            raise JobError("Το site μπλόκαρε το αίτημα (Cloudflare / 403). Δοκίμασε αργότερα.")
        raise JobError("Δεν κατέβηκε καμία εικόνα. Δες το κείμενο παραπάνω για το λόγο.")
    return finish_job(img_dir, name, title, out_dir, work_root, total, failed, mode, cb)


def blocked_msg(e):
    m = str(e)
    if "403" in m or "503" in m:
        return JobError("Το site μπλόκαρε το αίτημα (Cloudflare / 403). Δοκίμασε αργότερα.")
    return None


# ---------------------------------------------------------------- E-Hentai / ExHentai
class EHentaiEngine:
    def __init__(self, cb, cookie="", original=False):
        self.cb = cb
        self.f = Fetcher(cb)
        self.cookie = cookie
        self.original = original
        self.use_original = original
        self.logged_in = False
        self.need_note = False
        self.debug = ""
        self.host = "e-hentai.org"
        self.base = "https://e-hentai.org"
        self.cache = {}

    def _parse(self, url):
        m = re.search(r"https?://(?:www\.)?(e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]+)", url)
        if not m:
            raise JobError("Το link δεν μοιάζει με gallery του e-hentai.org")
        pairs = cookie_pairs(self.cookie)
        names = [k for k, _ in pairs]
        has_login = "ipb_member_id" in names and "ipb_pass_hash" in names
        if m.group(1) == "exhentai":
            self.host, self.base = "exhentai.org", "https://exhentai.org"
            if not has_login:
                raise JobError("Το ExHentai θέλει σύνδεση. Βάλε τα cookies (ipb_member_id, ipb_pass_hash, igneous) στο πεδίο «Cookies».")
        if has_login:
            self.logged_in = True
            for k, v in pairs:
                self.f.set_cookie(k, v, self.host)
        return m.group(2), m.group(3)

    @staticmethod
    def _next(txt):
        m = re.search(r'<a[^>]*id="next"[^>]*href="([^"]+)"', txt) or re.search(r'<a[^>]*href="([^"]+)"[^>]*id="next"', txt)
        return unescape(m.group(1)) if m else None

    def _collect(self, gid, token, delay):
        links, seen = [], set()
        total = None
        title = None
        page = 0
        while True:
            txt = self.f.text("%s/g/%s/%s/?p=%d" % (self.base, gid, token, page))
            if page == 0:
                m = re.search(r'<h1 id="gn">(.*?)</h1>', txt, re.S)
                if m:
                    title = unescape(m.group(1)).strip()
                m = re.search(r"Length:</td>\s*<td[^>]*>\s*(\d+)\s*pages", txt)
                if m:
                    total = int(m.group(1))
            new = 0
            for link in re.findall(r"https://" + re.escape(self.host) + r"/s/[0-9a-f]+/\d+-\d+", txt):
                if link not in seen:
                    seen.add(link)
                    links.append(link)
                    new += 1
            self.cb.log("Σελίδα previews %d: βρέθηκαν %d%s" % (page + 1, len(links), "/%d" % total if total else ""))
            if new == 0 or (total and len(links) >= total):
                break
            page += 1
            sleep(self.cb, delay)
        if not links:
            raise JobError("Δεν βρέθηκαν εικόνες. Τα cookies του ExHentai μάλλον λείπουν/έληξαν (χρειάζεται και το igneous)."
                           if self.host == "exhentai.org" else "Δεν βρέθηκαν εικόνες. Έλεγξε το link (ή το gallery έχει διαγραφεί).")
        if total and len(links) < total:
            self.cb.log("Τα previews σταμάτησαν νωρίτερα. Συνεχίζω ακολουθώντας το «επόμενο» κάθε εικόνας...")
            url = links[-1]
            while len(links) < total:
                txt = self.f.text(url)
                self.cache[url] = txt
                nxt = self._next(txt)
                if not nxt or nxt in seen:
                    break
                seen.add(nxt)
                links.append(nxt)
                url = nxt
                self.cb.log("Βρέθηκαν %d/%d" % (len(links), total))
                sleep(self.cb, delay)
        return title, links

    def _download_page(self, page_url, index, folder, use_cache):
        txt = self.cache.pop(page_url, None) if use_cache else None
        if txt is None:
            txt = self.f.text(page_url)
        if self.use_original:
            om = re.search(r'<a href="([^"]+)"[^>]*>\s*Download original', txt)
            if om:
                osrc = unescape(om.group(1))
                oext = os.path.splitext(osrc.split("?")[0])[1].lstrip(".").lower()
                if oext not in IMG_EXT:
                    oext = "jpg"
                odst = os.path.join(folder, "%04d.%s" % (index, oext))
                self.f.download(osrc, odst, referer=page_url)
                with open(odst, "rb") as fh:
                    head = fh.read(30000)
                if head[:1] != b"<":
                    return
                raw = head.decode("utf-8", "replace")
                low = raw.lower()
                plain = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
                plain = re.sub(r"<[^>]+>", " ", plain)
                self.debug = re.sub(r"\s+", " ", plain).strip()[:300]
                os.remove(odst)
                if "exceeded" in low or "viewing limit" in low or "image limit" in low:
                    raise LimitReached("Όριο εικόνων (image limit) του E-Hentai.")
                self.use_original = False
                self.need_note = True
        m = re.search(r'<img[^>]*id="img"[^>]*src="([^"]+)"', txt) or re.search(r'<img[^>]*src="([^"]+)"[^>]*id="img"', txt)
        if not m:
            raise JobError("δεν βρέθηκε η εικόνα στη σελίδα")
        src = unescape(m.group(1))
        name = src.split("?")[0].rsplit("/", 1)[-1]
        if name in ("509.gif", "509s.gif"):
            raise LimitReached("Όριο εικόνων (image limit) του E-Hentai.")
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        if ext not in IMG_EXT:
            ext = "jpg"
        self.f.download(src, os.path.join(folder, "%04d.%s" % (index, ext)), referer=page_url)

    def run(self, url, name_in, out_dir, work_root, mode, delay):
        cb = self.cb
        gid, token = self._parse(url)
        self.f.set_cookie("nw", "1", self.host)
        if self.original and not self.logged_in:
            cb.log("Για «original» το E-Hentai θέλει σύνδεση (cookies ipb_member_id + ipb_pass_hash). Χωρίς αυτά κατεβαίνει η κανονική ποιότητα.")
            self.use_original = False
        cb.log("== Ανάγνωση gallery ==")
        cb.progress(0, 0, "Ανάγνωση gallery...")
        title, links = self._collect(gid, token, delay)
        total = len(links)
        name = safe_name(name_in or title or "gallery_%s" % gid)
        cb.log("Τίτλος: %s | Σελίδες: %d" % (title or "-", total))
        img_dir = os.path.join(work_root, name)
        os.makedirs(img_dir, exist_ok=True)
        steps = steps_for(total, mode)
        cb.log("== Λήψη εικόνων μία-μία ==")
        failed = []
        for idx, link in enumerate(links):
            i = idx + 1
            cb.check()
            if already_have(img_dir, i):
                cb.log("%d/%d: υπάρχει ήδη, το προσπερνάω" % (i, total))
            else:
                try:
                    self._download_page(link, i, img_dir, True)
                    cb.log("%d/%d: OK" % (i, total))
                    if self.need_note:
                        self.need_note = False
                        cb.log("Το «Download original» δεν δούλεψε. Συνεχίζω με κανονική ποιότητα.")
                        cb.log("Απάντηση του site: %s" % (self.debug or "(κενή)"))
                        cb.log("Cookies που στάλθηκαν: %s" % ("ναι (ipb_member_id + ipb_pass_hash)" if self.logged_in else "όχι"))
                except (LimitReached, Cancelled):
                    raise
                except Exception as e:  # noqa: BLE001
                    failed.append(i)
                    cb.log("%d/%d: ΑΠΟΤΥΧΙΑ (%s)" % (i, total, e))
                sleep(cb, delay)
            cb.progress(i, steps, "Λήψη %d/%d" % (i, total))
        if failed:
            cb.log("== Δεύτερη προσπάθεια για %d σελίδες ==" % len(failed))
            still = []
            for i in failed:
                sleep(cb, max(delay, 1.0) * 2)
                try:
                    self._download_page(links[i - 1], i, img_dir, False)
                    cb.log("%d/%d: OK (2η προσπάθεια)" % (i, total))
                except (LimitReached, Cancelled):
                    raise
                except Exception as e:  # noqa: BLE001
                    still.append(i)
                    cb.log("%d/%d: ξανά ΑΠΟΤΥΧΙΑ (%s)" % (i, total, e))
            failed = still
        return finish_job(img_dir, name, title or name, out_dir, work_root, total, failed, mode, cb)


# ---------------------------------------------------------------- nHentai
class NHentaiEngine:
    def __init__(self, cb):
        self.cb = cb
        self.f = Fetcher(cb)

    def run(self, url, name_in, out_dir, work_root, mode, delay):
        cb = self.cb
        m = re.search(r"nhentai\.net/g/(\d+)", url)
        if not m:
            raise JobError("Το link δεν μοιάζει με gallery του nhentai.net")
        gid = m.group(1)
        cb.log("== Ανάγνωση gallery ==")
        try:
            data = self.f.data("https://nhentai.net/api/gallery/%s" % gid, referer="https://nhentai.net/")
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            raise blocked_msg(e) or JobError("Δεν μπόρεσα να διαβάσω το gallery (%s)." % e)
        try:
            obj = json.loads(data)
            media = str(obj["media_id"])
            pages = obj["images"]["pages"]
        except Exception:  # noqa: BLE001
            raise JobError("Το nhentai δεν επέστρεψε τα δεδομένα του gallery.")
        if not pages:
            raise JobError("Το nhentai δεν επέστρεψε τα δεδομένα του gallery.")
        name = safe_name(name_in or "nHentai_%s" % gid)
        cb.log("Σελίδες: %d" % len(pages))
        hosts = ["i.nhentai.net", "i1.nhentai.net", "i2.nhentai.net", "i3.nhentai.net", "i4.nhentai.net", "i5.nhentai.net", "i7.nhentai.net"]
        items = []
        for i, p in enumerate(pages):
            n = i + 1
            ext = LETTER_EXT.get(p.get("t", "j"), "jpg")
            items.append((n, ["https://%s/galleries/%s/%d.%s" % (h, media, n, ext) for h in hosts]))
        return download_items(items, self.f, name, name, out_dir, work_root, "https://nhentai.net/", mode, delay, cb)


# ---------------------------------------------------------------- HentaiFox / HentaiEra
class ImFamilyEngine:
    def __init__(self, cb, domain, label):
        self.cb = cb
        self.f = Fetcher(cb)
        self.domain = domain
        self.label = label

    @staticmethod
    def _input(id_, html):
        for p in (r'id="%s"[^>]*value="([^"]*)"' % id_, r'name="%s"[^>]*value="([^"]*)"' % id_, r'value="([^"]*)"[^>]*id="%s"' % id_):
            m = re.search(p, html)
            if m and m.group(1):
                return m.group(1)
        return None

    def run(self, url, name_in, out_dir, work_root, mode, delay):
        cb = self.cb
        domain = self.domain
        m = re.search(r"/gallery/(\d+)", url)
        if not m:
            raise JobError("Το link δεν μοιάζει με gallery του %s." % self.label)
        gid = m.group(1)
        cb.log("== Ανάγνωση gallery ==")
        try:
            html = self.f.text("https://%s/gallery/%s/" % (domain, gid), referer="https://%s/" % domain)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            raise blocked_msg(e) or JobError("Δεν μπόρεσα να ανοίξω το gallery (%s)." % e)
        exts = {}
        g = re.search(r"parseJSON\('(\{.*?\})'\)", html, re.S)
        if g:
            try:
                for k, v in json.loads(g.group(1)).items():
                    exts[int(k)] = LETTER_EXT.get(str(v).split(",")[0], "jpg")
            except Exception:  # noqa: BLE001
                pass
        d = self._input("load_dir", html)
        i = self._input("load_id", html)
        server = self._input("load_server", html)
        try:
            page_count = int(self._input("load_pages", html) or "")
        except ValueError:
            page_count = len(exts)
        thumb_host, thumb_ext = None, "jpg"
        if not d or not i or page_count == 0:
            t = re.search(r"//([a-z0-9.]+)/(\w+)/(\d+)/1t\.(\w+)", html)
            if t:
                thumb_host, d, i, thumb_ext = t.group(1), t.group(2), t.group(3), t.group(4)
            if page_count == 0:
                page_count = len(set(re.findall(r"/%s/(\d+)t\.\w+" % (i or gid), html)))
        if not d or not i or page_count <= 0:
            raise JobError("Δεν βρέθηκαν εικόνες. Η σελίδα του %s άλλαξε μορφή ή το gallery δεν υπάρχει." % self.label)
        name = safe_name(name_in or "%s_%s" % (self.label, gid))
        cb.log("Σελίδες: %d" % page_count)
        hosts = []
        if server:
            hosts.append("m%s.%s" % (server, domain))
        if thumb_host:
            hosts.append(thumb_host)
        hosts += ["i.%s" % domain, "i2.%s" % domain, "i3.%s" % domain]
        hosts = list(dict.fromkeys(hosts))
        items = []
        for n in range(1, page_count + 1):
            order = [exts.get(n, thumb_ext)]
            for e in ("jpg", "webp", "png", "gif"):
                if e not in order:
                    order.append(e)
            items.append((n, ["https://%s/%s/%s/%d.%s" % (h, d, i, n, e) for e in order for h in hosts[:2]]))
        return download_items(items, self.f, name, name, out_dir, work_root, "https://%s/" % domain, mode, delay, cb)


# ---------------------------------------------------------------- Hitomi.la
class HitomiEngine:
    def __init__(self, cb):
        self.cb = cb
        self.f = Fetcher(cb)

    @staticmethod
    def _parse_gg(js):
        def first(p):
            m = re.search(p, js)
            return m.group(1) if m else None
        b = first(r"b:\s*'(\d+)/?'") or first(r"gg\.b\s*=\s*'(\d+)/?'") or ""
        base = int(first(r"var o = (\d)") or 0)
        set_val = int(first(r"o = (\d);\s*break") or 1)
        cases = {int(x) for x in re.findall(r"case (\d+):", js)}
        return b, base, set_val, cases

    def run(self, url, name_in, out_dir, work_root, mode, delay):
        cb = self.cb
        m = re.search(r"(\d+)\.html", url) or re.search(r"/(\d+)(?:[/?#]|$)", url)
        if not m:
            raise JobError("Δεν βρήκα αριθμό gallery στο link του Hitomi.")
        gid = m.group(1)
        cb.log("== Ανάγνωση gallery ==")
        self.f.extra["Origin"] = "https://hitomi.la"
        info_text = None
        for host in ("ltn.gold-usergeneratedcontent.net", "ltn.hitomi.la"):
            try:
                info_text = self.f.text("https://%s/galleries/%s.js" % (host, gid), referer="https://hitomi.la/", retries=2)
                break
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                cb.log("%s: %s" % (host, e))
        try:
            files = json.loads(info_text[info_text.index("{"):])["files"]
            assert files
        except Exception:  # noqa: BLE001
            raise JobError("Δεν μπόρεσα να διαβάσω τα δεδομένα του gallery από το Hitomi.")
        gg_text = None
        for host in ("ltn.gold-usergeneratedcontent.net", "ltn.hitomi.la"):
            try:
                gg_text = self.f.text("https://%s/gg.js" % host, referer="https://hitomi.la/", retries=2)
                break
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                pass
        if not gg_text:
            raise JobError("Δεν μπόρεσα να διαβάσω το gg.js του Hitomi.")
        b, base, set_val, cases = self._parse_gg(gg_text)
        if not b:
            raise JobError("Το Hitomi άλλαξε μορφή (gg.js). Χρειάζεται ενημέρωση της εφαρμογής.")
        name = safe_name(name_in or "Hitomi.la_%s" % gid)
        cb.log("Σελίδες: %d" % len(files))
        items = []
        for idx, fl in enumerate(files):
            h = fl.get("hash") or ""
            if len(h) < 3:
                continue
            g = int(h[-1] + h[-3:-1], 16)
            o = set_val if g in cases else base
            items.append((idx + 1, [
                "https://w%d.gold-usergeneratedcontent.net/%s/%d/%s.webp" % (1 + o, b, g, h),
                "https://%sa.hitomi.la/webp/%s/%d/%s.webp" % ("a" if o == 0 else "b", b, g, h),
            ]))
        return download_items(items, self.f, name, name, out_dir, work_root, "https://hitomi.la/", mode, delay, cb)


# ---------------------------------------------------------------- e621
class E621Engine:
    def __init__(self, cb):
        self.cb = cb
        self.f = Fetcher(cb)
        self.f.s.headers["User-Agent"] = "GalleryToPDF-Windows/1.0 (personal use)"

    def _json(self, url):
        try:
            return json.loads(self.f.text(url))
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _file(post):
        pid = post.get("id", "?")
        file = post.get("file") or {}
        ext = (file.get("ext") or "").lower()
        if not ext:
            raise JobError("Το post %s δεν έχει αρχείο." % pid)
        if ext not in IMG_EXT:
            raise JobError("Το post %s είναι %s (βίντεο/flash). Υποστηρίζονται μόνο εικόνες." % (pid, ext))
        if not file.get("url"):
            raise JobError("Το post %s είναι κρυμμένο/μπλοκαρισμένο (χρειάζεται σύνδεση στο e621)." % pid)
        return file["url"]

    def run(self, url, name_in, out_dir, work_root, mode, delay):
        cb = self.cb
        cb.log("== Ανάγνωση e621 ==")
        items, title = [], ""
        m = re.search(r"e621\.net/pools/(\d+)", url)
        if m:
            pid = m.group(1)
            pool = self._json("https://e621.net/pools/%s.json" % pid)
            ids = pool.get("post_ids") or []
            if not ids:
                raise JobError("Δεν βρέθηκε το pool %s." % pid)
            title = (pool.get("name") or "e621_pool_%s" % pid).replace("_", " ")
            cb.log("Pool: %s | Εικόνες: %d" % (title, len(ids)))
            by_id, page = {}, 1
            while len(by_id) < len(ids) and page <= 50:
                posts = self._json("https://e621.net/posts.json?tags=pool:%s&limit=320&page=%d" % (pid, page)).get("posts") or []
                if not posts:
                    break
                for p in posts:
                    by_id[p.get("id")] = p
                page += 1
                sleep(cb, max(delay, 0.6))
            n = 0
            for i in ids:
                p = by_id.get(i)
                if not p:
                    cb.log("Παράλειψη post %s (δεν επέστρεψε το API)" % i)
                    continue
                try:
                    u = self._file(p)
                    n += 1
                    items.append((n, [u]))
                except JobError as e:
                    cb.log(str(e))
        else:
            m = re.search(r"e621\.net/posts/(\d+)", url)
            if not m:
                raise JobError("Βάλε link από ένα post (e621.net/posts/123) ή pool (e621.net/pools/123).")
            post = self._json("https://e621.net/posts/%s.json" % m.group(1)).get("post")
            if not post:
                raise JobError("Δεν βρέθηκε το post %s." % m.group(1))
            items = [(1, [self._file(post)])]
            title = "e621_%s" % m.group(1)
        if not items:
            raise JobError("Δεν βρέθηκαν εικόνες για λήψη.")
        name = safe_name(name_in or title)
        return download_items(items, self.f, name, name, out_dir, work_root, "https://e621.net/", mode, delay, cb)


# ---------------------------------------------------------------- Rule34.xyz
class Rule34XYZEngine:
    def __init__(self, cb):
        self.cb = cb
        self.f = Fetcher(cb)

    def run(self, url, name_in, out_dir, work_root, mode, delay):
        cb = self.cb
        m = re.search(r"rule34\.xyz/post/(\d+)", url)
        if not m:
            raise JobError("Βάλε link από ένα post (rule34.xyz/post/123).")
        pid = m.group(1)
        cb.log("== Ανάγνωση Rule34.xyz ==")
        d = "https://rule34xyz.b-cdn.net/posts/%d/%s/%s" % (int(pid) // 1000, pid, pid)
        cands = []
        try:
            html = self.f.text(url, referer="https://rule34.xyz/", retries=2)
            for u in re.findall(r"https://rule34xyz\.b-cdn\.net/posts/[^\"'\s<>]+?\.pic\.(?:jpg|jpeg|png|webp|gif)", html):
                if u not in cands:
                    cands.append(u)
            if not cands and re.search(r"\.(?:mp4|webm)", html) and not re.search(r"\.pic\.", html):
                cb.log("Σημείωση: το post μοιάζει με βίντεο (δοκίμασε το link στο «Βίντεο»).")
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001
            cb.log("Η σελίδα δεν άνοιξε· δοκιμάζω απευθείας το CDN.")
        for ext in ("jpg", "png", "webp", "jpeg", "gif"):
            u = "%s.pic.%s" % (d, ext)
            if u not in cands:
                cands.append(u)
        name = safe_name(name_in or "rule34xyz_%s" % pid)
        return download_items([(1, cands)], self.f, name, name, out_dir, work_root, "https://rule34.xyz/", mode, delay, cb)


# ---------------------------------------------------------------- Video (yt-dlp)
def _ffmpeg_path():
    exe = os.path.join(os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__), "ffmpeg.exe")
    if os.path.exists(exe):
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


class VideoEngine:
    def __init__(self, cb):
        self.cb = cb

    def run(self, url, name_in, out_dir, work_root, max_height):
        cb = self.cb
        try:
            import yt_dlp
        except ImportError:
            raise JobError("Λείπει το yt-dlp από αυτή την εγκατάσταση.")
        os.makedirs(out_dir, exist_ok=True)
        job = os.path.join(work_root, "video_" + uuid.uuid4().hex)
        os.makedirs(job, exist_ok=True)
        cb.log("== Βίντεο (yt-dlp) ==")
        cb.progress(0, 0, "Ανάγνωση βίντεο...")

        class _L:
            def debug(s, m):
                pass

            def info(s, m):
                pass

            def warning(s, m):
                cb.log("⚠ " + m)

            def error(s, m):
                cb.log("✖ " + m)

        def hook(d):
            cb.check()
            if d.get("status") == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                if tot:
                    cb.progress(int(got * 900 / tot), 1000, "Λήψη %d%% (%d MB)" % (got * 100 // tot, got >> 20))
                else:
                    cb.progress(0, 0, "Λήψη %d MB" % (got >> 20))
            elif d.get("status") == "finished":
                cb.log("Ολοκληρώθηκε η λήψη ενός αρχείου.")

        h = int(max_height)
        opts = {
            "format": "bv*[height<=%d]+ba/b[height<=%d]/bv*+ba/b" % (h, h),
            "format_sort": ["res:%d" % h, "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(job, "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            "logger": _L(),
            "progress_hooks": [hook],
            "restrictfilenames": False,
            "windowsfilenames": True,
            "concurrent_fragment_downloads": 4,
            "retries": 5,
        }
        ff = _ffmpeg_path()
        if ff:
            opts["ffmpeg_location"] = ff
        else:
            cb.log("⚠ Δεν βρέθηκε ffmpeg: βίντεο και ήχος μπορεί να μείνουν ξεχωριστά.")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if self.cb.cancel.is_set():
                raise Cancelled()
            raise JobError("yt-dlp: %s" % re.sub(r"\x1b\[[0-9;]*m", "", str(e)))
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), info)
        files = [os.path.join(job, n) for n in os.listdir(job) if not n.endswith((".part", ".ytdl"))]
        if not files:
            raise JobError("Δεν δημιουργήθηκε αρχείο.")
        files.sort(key=lambda p: os.path.getsize(p), reverse=True)
        main = files[0]
        ext = os.path.splitext(main)[1]
        title = (info or {}).get("title") or "video"
        name = safe_name(name_in or title)
        dst = os.path.join(out_dir, name + ext)
        n = 1
        while os.path.exists(dst):
            n += 1
            dst = os.path.join(out_dir, "%s (%d)%s" % (name, n, ext))
        shutil.move(main, dst)
        shutil.rmtree(job, ignore_errors=True)
        cb.progress(1000, 1000, "Έτοιμο ✔")
        cb.log("Έτοιμο: %s" % os.path.basename(dst))
        return [dst]


# ---------------------------------------------------------------- DeviantArt (video)
class DeviantArtEngine:
    def __init__(self, cb):
        self.cb = cb
        self.f = Fetcher(cb)

    @staticmethod
    def _clean(s):
        return s.replace("\\u002F", "/").replace("\\u0026", "&").replace("\\/", "/").replace("&amp;", "&")

    def _videos(self, raw):
        t = self._clean(raw)
        out = []
        for u in re.findall(r"https?://[^\"'\s<>\\)]+?\.(?:mp4|webm|m4v)(?:\?[^\"'\s<>\\)]*)?", t, re.I):
            if u not in out:
                out.append(u)
        return out

    def run(self, url, name_in, out_dir, work_root):
        cb = self.cb
        cb.log("== DeviantArt βίντεο ==")
        m = re.search(r"-(\d{6,})(?:[/?#]|$)", url) or re.search(r"(\d{6,})", url)
        did = m.group(1) if m else ""
        um = re.search(r"deviantart\.com/([^/]+)/", url)
        user = um.group(1) if um else ""
        cb.log("Deviation id: %s" % did)
        title, html, cands = "", "", []
        try:
            html = self.f.text(url)
            tm = re.search(r"<title>([^<]+)</title>", html)
            title = unescape(tm.group(1)) if tm else ""
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            cb.log("Η σελίδα δεν ανοίγει: %s" % e)
        if did:
            api = ("https://www.deviantart.com/_napi/da-browse/shared_api/deviation/extended_fetch?deviationid=%s&username=%s&type=art&include_session=false"
                   % (did, user))
            try:
                j = self.f.text(api, referer=url)
                cands += self._videos(j)
                cb.log("Βίντεο από API: %d" % len(cands))
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                cb.log("API σφάλμα: %s" % e)
        if not cands and html and did:
            t = self._clean(html)
            for pat in ('"deviationId":%s' % did, '"deviationId":"%s"' % did):
                pos = 0
                while True:
                    k = t.find(pat, pos)
                    if k < 0:
                        break
                    depth, i, start = 0, k, -1
                    while i >= 0:
                        c = t[i]
                        if c == "}":
                            depth += 1
                        elif c == "{":
                            if depth == 0:
                                start = i
                                break
                            depth -= 1
                        i -= 1
                    if start >= 0:
                        depth, e, end = 0, start, -1
                        while e < len(t):
                            c = t[e]
                            if c == "{":
                                depth += 1
                            elif c == "}":
                                depth -= 1
                                if depth == 0:
                                    end = e
                                    break
                            e += 1
                        if end > start:
                            for u in self._videos(t[start:end + 1]):
                                if u not in cands:
                                    cands.append(u)
                    pos = k + len(pat)
            cb.log("Βίντεο από τη σελίδα: %d" % len(cands))
        if not cands:
            raise JobError("Δεν βρήκα βίντεο σε αυτό το link DeviantArt. Δοκίμασε το κουμπί Download του DeviantArt (με login) ή δώσε απευθείας URL .mp4.")
        sized = []
        for c in cands[:12]:
            size = -1
            try:
                r = self.f.s.get(c, headers={"Range": "bytes=0-0", "Referer": "https://www.deviantart.com/"}, timeout=20, stream=True)
                cr = r.headers.get("Content-Range")
                size = int(cr.split("/")[-1]) if cr else int(r.headers.get("Content-Length") or -1)
                cb.log("• HTTP %d %d KB %s" % (r.status_code, size // 1024, c[-80:]))
                r.close()
            except Exception as e:  # noqa: BLE001
                cb.log("• δεν ανοίγει: %s" % c[-80:])
            sized.append((size, c))
        size, best = max(sized)
        if size < 200_000:
            raise JobError("Βρέθηκαν μόνο μικρά/άκυρα αρχεία βίντεο (δες το log).")
        ext = (re.search(r"\.(mp4|webm|m4v)(?:\?|$)", best, re.I) or [None, "mp4"])[1].lower()
        name = safe_name(name_in or title or "deviantart_video")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, name + "." + ext)
        cb.log("Λήψη βίντεο (%d MB)..." % (size >> 20))
        self.f.download(best, dst, referer="https://www.deviantart.com/",
                        progress=lambda g, t: cb.progress(int(g * 1000 / t) if t else 0, 1000 if t else 0, "Λήψη %d MB" % (g >> 20)))
        cb.progress(1000, 1000, "Έτοιμο ✔")
        return [dst]


# ---------------------------------------------------------------- routing
SITES = [
    ("E-Hentai", "ehentai", ["e-hentai.org"]),
    ("ExHentai", "ehentai", ["exhentai.org"]),
    ("e621", "e621", ["e621.net"]),
    ("Rule34.xyz", "rule34xyz", ["rule34.xyz"]),
    ("DeviantArt (βίντεο)", "deviantart", ["deviantart.com"]),
    ("nHentai", "nhentai", ["nhentai.net"]),
    ("HentaiFox", "imfamily", ["hentaifox.com"]),
    ("Hitomi.la", "hitomi", ["hitomi.la"]),
    ("HentaiEra", "imfamily", ["hentaiera.com"]),
]


def detect_site(url):
    low = (url or "").strip().lower()
    if not low.startswith("http") or len(low) > 600 or "\n" in low:
        return None
    for name, engine, hosts in SITES:
        if any(h in low for h in hosts):
            return name, engine, hosts
    return "Βίντεο (yt-dlp)", "ytdlp", []


def run_job(url, name, out_dir, work_root, mode, delay, cookie, quality, original, cb):
    url = (url or "").strip()
    site = detect_site(url)
    if not site:
        raise JobError("Δεν αναγνώρισα το site αυτού του link.")
    sname, engine, hosts = site
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(work_root, exist_ok=True)
    try:
        if engine == "ehentai":
            return EHentaiEngine(cb, cookie, original).run(url, name, out_dir, work_root, mode, delay)
        if engine == "nhentai":
            return NHentaiEngine(cb).run(url, name, out_dir, work_root, mode, delay)
        if engine == "imfamily":
            return ImFamilyEngine(cb, hosts[0], sname).run(url, name, out_dir, work_root, mode, delay)
        if engine == "hitomi":
            return HitomiEngine(cb).run(url, name, out_dir, work_root, mode, delay)
        if engine == "e621":
            return E621Engine(cb).run(url, name, out_dir, work_root, mode, delay)
        if engine == "rule34xyz":
            return Rule34XYZEngine(cb).run(url, name, out_dir, work_root, mode, delay)
        if engine == "deviantart":
            return DeviantArtEngine(cb).run(url, name, out_dir, work_root)
        return VideoEngine(cb).run(url, name, out_dir, work_root, quality)
    except LimitReached as e:
        raise JobError("%s Περίμενε λίγη ώρα (ή άλλαξε IP) και ξαναπάτα «Κατέβασε»: οι εικόνες που κατέβηκαν μένουν." % e)
