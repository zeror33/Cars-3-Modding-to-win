#!/usr/bin/env python3
"""
Cars 3: Driven to Win — Ultimate Live Editor Backend
Serves file browsing, editing, OCT/BENT parsing, texture decoding, search,
and 3D model extraction for Cars 3 characters from bundled game assets.
"""

import http.server
import json
import os
import sys
import io
import math
import struct
import urllib.parse
import zipfile
import base64
import traceback
import socketserver
import hashlib
import webbrowser
from collections import defaultdict
from http import HTTPStatus

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
EXEFS_DIR = os.path.join(PROJECT_DIR, "exefs")
ROMFS_DIR = os.path.join(PROJECT_DIR, "romfs")

# ── RevOctane ──────────────────────────────────────────
sys.path.insert(0, os.path.join(PROJECT_DIR, "RevOctane"))
try:
    import bstream
    import revoctane
    HAS_REVOCTANE = True
except ImportError:
    HAS_REVOCTANE = False
    print("⚠ RevOctane modules not available — OCT parsing disabled")

# ── PIL ────────────────────────────────────────────────
try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠ PIL not available — texture decoding fallback only")

PORT = 8765


# ════════════════════════════════════════════════════════
#  TEXTURE DECODING (BC1 / BC3 block compression)
# ════════════════════════════════════════════════════════

def _unpack_565(v):
    return (
        ((v >> 11) & 0x1F) * 255 // 31,
        ((v >> 5) & 0x3F) * 255 // 63,
        (v & 0x1F) * 255 // 31,
    )


def _interp3(c0, c1, n):
    if n == 0:
        return c0
    if n == 1:
        return c1
    return tuple(c0[i] + (c1[i] - c0[i]) * n // 3 for i in range(3))


def _decode_bc3(blk, w, h, x0, y0, px):
    c0 = _unpack_565(struct.unpack_from("<H", blk, 0)[0])
    c1 = _unpack_565(struct.unpack_from("<H", blk, 2)[0])
    pal = [c0, c1, _interp3(c0, c1, 2), _interp3(c0, c1, 3)]
    a0, a1 = blk[8], blk[9]
    ap = [a0, a1]
    if a0 > a1:
        for i in range(1, 7):
            ap.append((a0 * (7 - i) + a1 * i) // 7)
    else:
        for i in range(1, 5):
            ap.append((a0 * (5 - i) + a1 * i) // 5)
        ap += [0, 255]
    ai = struct.unpack_from("<Q", blk, 10)[0]
    ci = struct.unpack_from("<Q", blk, 4)[0]
    for py in range(4):
        for pxx in range(4):
            xx, yy = x0 + pxx, y0 + py
            if xx >= w or yy >= h:
                continue
            c = pal[(ci >> (py * 12 + pxx * 2)) & 3]
            a = ap[(ai >> (py * 12 + pxx * 2)) & 3]
            px[yy * w + xx] = (c[0], c[1], c[2], a)


def _decode_bc1(blk, w, h, x0, y0, px):
    c0 = _unpack_565(struct.unpack_from("<H", blk, 0)[0])
    c1 = _unpack_565(struct.unpack_from("<H", blk, 2)[0])
    ci = struct.unpack_from("<Q", blk, 4)[0]
    if struct.unpack_from("<H", blk, 0)[0] > struct.unpack_from("<H", blk, 2)[0]:
        pal = [c0, c1, _interp3(c0, c1, 2), _interp3(c0, c1, 3)]
    else:
        pal = [c0, c1, tuple((c0[i] + c1[i]) // 2 for i in range(3)), (255, 255, 255)]
    for py in range(4):
        for pxx in range(4):
            xx, yy = x0 + pxx, y0 + py
            if xx >= w or yy >= h:
                continue
            c = pal[(ci >> (py * 12 + pxx * 2)) & 3]
            px[yy * w + xx] = (c[0], c[1], c[2], 255)


def decode_tbody(data):
    """Decode a .tbody texture (BC1/BC3 compressed) to RGBA pixel list."""
    if len(data) < 32:
        return None, 0, 0
    w = struct.unpack_from("<H", data, 4)[0]
    h = struct.unpack_from("<H", data, 6)[0]
    fmt = struct.unpack_from("<I", data, 8)[0]
    doff = struct.unpack_from("<I", data, 12)[0]
    if w == 0 or h == 0 or w > 4096 or h > 4096 or doff > len(data):
        return None, 0, 0
    px = [(0, 0, 0, 0)] * (w * h)
    if fmt in (0x43445333, 3):
        bs, decoder = 16, _decode_bc3
    elif fmt in (0x31534443, 1):
        bs, decoder = 8, _decode_bc1
    else:
        return None, 0, 0
    stride = (w + 3) // 4 * bs
    for by in range((h + 3) // 4):
        for bx in range((w + 3) // 4):
            o = doff + by * stride + bx * bs
            if o + bs > len(data):
                break
            decoder(data[o : o + bs], w, h, bx * 4, by * 4, px)
    return px, w, h


# ════════════════════════════════════════════════════════
#  HTTP HANDLER
# ════════════════════════════════════════════════════════

class Cars3APIHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PROJECT_DIR, **kwargs)

    # ── helpers ────────────────────────────────────────

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    @staticmethod
    def _sanitize(obj):
        """Recursively replace NaN/Inf with 0 for valid JSON."""
        if isinstance(obj, float):
            if obj != obj or obj == float("inf") or obj == float("-inf"):
                return 0.0
            return obj
        if isinstance(obj, dict):
            return {k: Cars3APIHandler._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [Cars3APIHandler._sanitize(v) for v in obj]
        return obj

    def _json_response(self, data, status=200):
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(self._sanitize(data)).encode("utf-8"))

    def _resolve_path(self, filepath):
        filepath = filepath.lstrip("/")
        bases = {
            "exefs": EXEFS_DIR,
            "romfs": ROMFS_DIR,
            "RevOctane": os.path.join(PROJECT_DIR, "RevOctane"),
        }
        for prefix, base_dir in bases.items():
            if filepath == prefix or filepath.startswith(prefix + "/"):
                rel = filepath[len(prefix) :].lstrip("/")
                full = os.path.normpath(os.path.join(base_dir, rel))
                if full.startswith(os.path.normpath(base_dir)):
                    return full, prefix
            elif filepath.startswith(prefix):
                rel = filepath[len(prefix) :]
                full = os.path.normpath(os.path.join(base_dir, rel.lstrip("/")))
                if full.startswith(os.path.normpath(base_dir)):
                    return full, prefix
        if filepath in ("", "."):
            return None, "root"
        return None, None

    def _is_binary(self, data):
        if len(data) == 0:
            return False
        textchars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})
        return bool(data.translate(None, textchars))

    def _guess_mime(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {
            ".html": "text/html",
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".zip": "application/zip",
            ".txt": "text/plain",
            ".lua": "text/x-lua",
            ".py": "text/x-python",
            ".xml": "text/xml",
        }.get(ext, "application/octet-stream")

    def _get_item_info(self, virtual_path, full_path):
        info = {
            "name": os.path.basename(full_path),
            "path": virtual_path,
            "type": "dir" if os.path.isdir(full_path) else "file",
        }
        if os.path.isfile(full_path):
            info["size"] = os.path.getsize(full_path)
            info["ext"] = os.path.splitext(full_path)[1].lower()
        return info

    # ── routing ────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            routes = {
                "/api/tree": lambda: self._handle_tree(params),
                "/api/file": lambda: self._handle_read_file(params),
                "/api/parse/oct": lambda: self._handle_parse_oct(params),
                "/api/parse/bent": lambda: self._handle_parse_bent(params),
                "/api/bundles": lambda: self._handle_list_bundles(),
                "/api/bundle": lambda: self._handle_bundle_contents(params),
                "/api/search": lambda: self._handle_search(params),
                "/api/texture": lambda: self._handle_texture(params),
                "/api/fileinfo": lambda: self._handle_file_info(params),
                "/api/model/view": lambda: self._handle_model_view(params),
                "/api/dirinfo": lambda: self._handle_dir_info(params),
                "/api/characters": lambda: self._handle_characters(params),
                "/api/character/model": lambda: self._handle_character_model(params),
            }
            handler = routes.get(path)
            if handler:
                handler()
            elif path == "/":
                self._serve_frontend()
            elif path.endswith(".html") or path.endswith(".js") or path.endswith(".css"):
                self._serve_static(path)
            else:
                super().do_GET()
        except Exception as e:
            self._json_response({"error": str(e), "traceback": traceback.format_exc()}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            if path == "/api/file":
                self._handle_write_file(params, body)
            elif path == "/api/bundle/extract":
                self._handle_extract_bundle(params, body)
            else:
                self._json_response({"error": "Not found"}, 404)
        except Exception as e:
            self._json_response({"error": str(e), "traceback": traceback.format_exc()}, 500)

    # ════════════════════════════════════════════════════
    #  API ENDPOINTS
    # ════════════════════════════════════════════════════

    def _handle_tree(self, params):
        base_path = params.get("path", ["romfs"])[0]
        resolved, prefix = self._resolve_path(base_path)
        if base_path == "root":
            root_items = []
            for name in ("exefs", "romfs", "RevOctane"):
                fp = os.path.join(PROJECT_DIR, name)
                if os.path.exists(fp):
                    root_items.append(self._get_item_info(name, fp))
            self._json_response({"name": "root", "type": "dir", "children": root_items})
            return
        if not resolved or not os.path.exists(resolved):
            self._json_response({"error": "Path not found"}, 404)
            return
        if os.path.isfile(resolved):
            self._json_response({"error": "Not a directory"}, 400)
            return
        items = []
        try:
            for name in sorted(os.listdir(resolved)):
                if name.startswith("._"):
                    continue
                full = os.path.join(resolved, name)
                virtual = f"{base_path}/{name}" if base_path != "root" else name
                items.append(self._get_item_info(virtual, full))
        except PermissionError:
            self._json_response({"error": "Permission denied"}, 403)
            return
        self._json_response({
            "name": os.path.basename(resolved) or base_path,
            "type": "dir",
            "path": base_path,
            "children": items,
        })

    def _handle_read_file(self, params):
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.isfile(resolved):
            self._json_response({"error": "File not found"}, 404)
            return
        size = os.path.getsize(resolved)
        ext = os.path.splitext(resolved)[1].lower()
        with open(resolved, "rb") as f:
            data = f.read()

        # OCT files
        if ext in (".oct", ".bent", ".bct") and HAS_REVOCTANE:
            try:
                stream = bstream.BStream(file=resolved)
                obj = revoctane.Octane(stream)
                if obj._state != "fail":
                    parsed = {
                        "type": "oct", "size": size,
                        "hex": data.hex()[:32000],
                        "lines": [], "strings": obj._strings,
                    }
                    for line in obj._lines[1:]:
                        dr = line.data
                        if isinstance(dr, bytes):
                            dr = f"<bytes: {len(dr)}>"
                        elif isinstance(dr, list):
                            dr = f"[{len(dr)} items]"
                        elif dr is None:
                            dr = ""
                        else:
                            dr = str(dr)
                        parsed["lines"].append({
                            "indent": line.indent,
                            "format": f"0x{line.format:04X}",
                            "name": line.name,
                            "data": dr,
                        })
                    self._json_response(parsed)
                    return
            except Exception:
                pass

        # Texture (tbody)
        if ext == ".tbody":
            px, w, h = decode_tbody(data)
            if px:
                if HAS_PIL:
                    img = PILImage.new("RGBA", (w, h))
                    img.putdata(px)
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    self._json_response({
                        "type": "texture", "width": w, "height": h,
                        "size": size, "dataUri": f"data:image/png;base64,{b64}",
                    })
                    return
                else:
                    raw = b"".join(bytes([r, g, b_, a]) for r, g, b_, a in px)
                    self._json_response({
                        "type": "texture", "width": w, "height": h,
                        "size": size, "rawPixels": base64.b64encode(raw).decode(),
                        "format": "RGBA",
                    })
                    return

        # ZIP / bundles
        if ext == ".zip":
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                entries = []
                for name in zf.namelist():
                    info = zf.getinfo(name)
                    entries.append({
                        "name": name,
                        "size": info.file_size,
                        "compressed": info.compress_size,
                        "date": f"{info.date_time[0]:04d}-{info.date_time[1]:02d}-{info.date_time[2]:02d}" if info.date_time else "",
                    })
                zf.close()
                self._json_response({"type": "zip", "size": size, "entries": entries, "entryCount": len(entries)})
                return
            except Exception:
                pass

        # BENT
        if ext == ".bent":
            parsed = self._parse_bent_data(data)
            if parsed.get("valid"):
                self._json_response({
                    "type": "bent", "size": size,
                    "hex": data[:4096].hex(),
                    "version": parsed.get("version", ""),
                    "strings": parsed.get("strings", []),
                    "channels": parsed.get("channels", []),
                })
                return

        # ExeFS binaries
        if ext in (".nso", ".npdm") or filepath.startswith("exefs/"):
            self._json_response({
                "type": "binary", "size": size,
                "hex": data[:4096].hex(),
                "info": {"size": size, "md5": hashlib.md5(data).hexdigest()},
            })
            return

        # Text fallback
        if not self._is_binary(data):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            self._json_response({"type": "text", "size": size, "content": text})
            return

        # Generic binary
        self._json_response({"type": "binary", "size": size, "hex": data[:4096].hex(), "info": {"size": size}})

    def _parse_bent_data(self, data):
        result = {"version": "", "strings": [], "channels": [], "valid": False}
        if len(data) < 16:
            return result
        magic = struct.unpack_from("<H", data, 0)[0]
        if magic != 0x7629:
            return result
        result["valid"] = True
        for i in range(min(len(data), 4096)):
            if data[i : i + 5] in (b"Versi", b"versi"):
                end = i
                while end < len(data) and data[end] != 0:
                    end += 1
                result["version"] = data[i:end].decode("utf-8", errors="replace")
                break
        strings = []
        pos = 0x40 if result["version"] else 0x40
        while pos < len(data) - 1:
            if data[pos] == 0:
                pos += 1
                continue
            s_end = pos
            while s_end < len(data) and data[s_end] != 0:
                s_end += 1
            if s_end > pos:
                s = data[pos:s_end].decode("utf-8", errors="replace")
                if len(s) > 1 and not all(c < " " for c in s):
                    strings.append(s)
            pos = s_end + 1
            if len(strings) > 500:
                break
        result["strings"] = strings
        result["channels"] = [
            s for s in strings
            if "/" not in s and "." not in s and len(s) < 40
            and not s.startswith("shared_") and not s.startswith("shrd_")
        ]
        return result

    def _handle_write_file(self, params, body):
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.exists(resolved):
            self._json_response({"error": "File not found"}, 404)
            return
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, 400)
            return
        if "content" in payload:
            content = payload["content"]
            if payload.get("encoding") == "base64":
                with open(resolved, "wb") as f:
                    f.write(base64.b64decode(content))
            else:
                with open(resolved, "w", encoding="utf-8") as f:
                    f.write(content)
            self._json_response({"success": True, "size": os.path.getsize(resolved)})
        elif "hex" in payload:
            with open(resolved, "wb") as f:
                f.write(bytes.fromhex(payload["hex"]))
            self._json_response({"success": True, "size": os.path.getsize(resolved)})
        else:
            self._json_response({"error": "No content provided"}, 400)

    def _handle_parse_oct(self, params):
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.isfile(resolved):
            self._json_response({"error": "File not found"}, 404)
            return
        if not HAS_REVOCTANE:
            self._json_response({"error": "RevOctane not available"})
            return
        try:
            stream = bstream.BStream(file=resolved)
            obj = revoctane.Octane(stream)
            result = {"state": "ok" if obj._state != "fail" else "fail", "strings": obj._strings, "lines": []}
            for line in obj._lines[1:]:
                dr = line.data
                if isinstance(dr, bytes):
                    dr = f"<bytes: {len(dr)}>"
                elif isinstance(dr, list):
                    dr = f"[{len(dr)} items]"
                elif dr is None:
                    dr = ""
                else:
                    dr = str(dr)
                result["lines"].append({"indent": line.indent, "format": f"0x{line.format:04X}", "name": line.name, "data": dr})
            self._json_response(result)
        except Exception as e:
            self._json_response({"error": str(e)})

    def _handle_parse_bent(self, params):
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.isfile(resolved):
            self._json_response({"error": "File not found"}, 404)
            return
        with open(resolved, "rb") as f:
            data = f.read()
        result = self._parse_bent_data(data)
        result["size"] = len(data)
        result["hex"] = data[:2048].hex()
        self._json_response(result)

    def _handle_list_bundles(self):
        bundles = []
        bundles_dir = os.path.join(ROMFS_DIR, "bundles")
        if os.path.exists(bundles_dir):
            for name in sorted(os.listdir(bundles_dir)):
                if name.startswith("._"):
                    continue
                full = os.path.join(bundles_dir, name)
                if os.path.isdir(full):
                    zp = os.path.join(full, "_root_.zip")
                    if not os.path.exists(zp):
                        zips = [f for f in os.listdir(full) if f.endswith(".zip")]
                        if not zips:
                            continue
                        zp = os.path.join(full, zips[0])
                    bundles.append({
                        "name": name,
                        "path": f"romfs/bundles/{name}",
                        "zipPath": os.path.relpath(zp, ROMFS_DIR),
                        "size": os.path.getsize(zp),
                    })
        self._json_response({"bundles": bundles})

    def _handle_bundle_contents(self, params):
        name = params.get("name", [""])[0]
        bundles_dir = os.path.join(ROMFS_DIR, "bundles", name)
        if not os.path.isdir(bundles_dir):
            self._json_response({"error": "Bundle not found"}, 404)
            return
        zp = os.path.join(bundles_dir, "_root_.zip")
        if not os.path.exists(zp):
            zips = [f for f in os.listdir(bundles_dir) if f.endswith(".zip")]
            if not zips:
                items = []
                for root, _dirs, files in os.walk(bundles_dir):
                    for f in files:
                        items.append(os.path.relpath(os.path.join(root, f), bundles_dir))
                self._json_response({"name": name, "type": "dir", "files": items})
                return
            zp = os.path.join(bundles_dir, zips[0])
        try:
            zf = zipfile.ZipFile(zp, "r")
            entries = []
            for zi in zf.infolist():
                entries.append({
                    "name": zi.filename,
                    "size": zi.file_size,
                    "compressed": zi.compress_size,
                    "date": f"{zi.date_time[0]:04d}-{zi.date_time[1]:02d}-{zi.date_time[2]:02d}" if zi.date_time else "",
                })
            zf.close()
            self._json_response({
                "name": name,
                "zipPath": os.path.relpath(zp, ROMFS_DIR),
                "entries": entries,
                "entryCount": len(entries),
                "zipSize": os.path.getsize(zp),
            })
        except Exception as e:
            self._json_response({"error": str(e)})

    def _handle_search(self, params):
        query = params.get("q", [""])[0].lower()
        if not query:
            self._json_response({"error": "No query"}, 400)
            return
        results = []
        search_dirs = [("exefs", EXEFS_DIR), ("romfs", ROMFS_DIR)]
        for base_name, base_dir in search_dirs:
            if not os.path.exists(base_dir):
                continue
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                for f in files:
                    if f.startswith("._"):
                        continue
                    if query in f.lower():
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, base_dir)
                        results.append({
                            "name": f,
                            "path": f"{base_name}/{rel}",
                            "size": os.path.getsize(full) if os.path.isfile(full) else 0,
                            "base": base_name,
                        })
                        if len(results) >= 200:
                            break
                if len(results) >= 200:
                    break
        self._json_response({"results": results, "count": len(results)})

    def _handle_texture(self, params):
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.isfile(resolved):
            self._json_response({"error": "File not found"}, 404)
            return
        with open(resolved, "rb") as f:
            data = f.read()
        px, w, h = decode_tbody(data)
        if not px:
            self._json_response({"error": "Could not decode texture"})
            return
        if HAS_PIL:
            img = PILImage.new("RGBA", (w, h))
            img.putdata(px)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            self._json_response({
                "width": w, "height": h,
                "image": base64.b64encode(buf.getvalue()).decode(),
            })
        else:
            raw = b"".join(bytes([r, g, b_, a]) for r, g, b_, a in px)
            self._json_response({
                "width": w, "height": h,
                "pixels": base64.b64encode(raw).decode(),
                "format": "RGBA",
            })

    def _handle_file_info(self, params):
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.exists(resolved):
            self._json_response({"error": "Path not found"}, 404)
            return
        stat = os.stat(resolved)
        info = {
            "name": os.path.basename(resolved),
            "path": filepath,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "isDir": os.path.isdir(resolved),
            "ext": os.path.splitext(resolved)[1].lower() if os.path.isfile(resolved) else "",
        }
        if os.path.isfile(resolved):
            info["mime"] = self._guess_mime(resolved)
        self._json_response(info)

    def _handle_dir_info(self, params):
        dirpath = params.get("path", ["romfs"])[0]
        resolved, _ = self._resolve_path(dirpath)
        if not resolved or not os.path.isdir(resolved):
            self._json_response({"error": "Directory not found"}, 404)
            return
        entries = []
        for name in sorted(os.listdir(resolved)):
            if name.startswith("._"):
                continue
            full = os.path.join(resolved, name)
            entries.append({
                "name": name,
                "path": f"{dirpath}/{name}",
                "type": "dir" if os.path.isdir(full) else "file",
                "size": os.path.getsize(full) if os.path.isfile(full) else 0,
            })
        self._json_response(entries)

    def _handle_characters(self, params):
        """List all Cars 3 characters with renderability info."""
        bundles_dir = os.path.join(ROMFS_DIR, "bundles")
        characters = []
        if not os.path.isdir(bundles_dir):
            self._json_response({"characters": [], "count": 0})
            return
        for name in sorted(os.listdir(bundles_dir)):
            if name.startswith("._") or not name.startswith("actor-cars3_"):
                continue
            full_dir = os.path.join(bundles_dir, name)
            if not os.path.isdir(full_dir):
                continue
            zp = os.path.join(full_dir, "_root_.zip")
            has_zip = os.path.exists(zp)
            if not has_zip:
                zips = [f for f in os.listdir(full_dir) if f.endswith(".zip")]
                if zips:
                    zp = os.path.join(full_dir, zips[0])
                    has_zip = True
            if not has_zip:
                continue
            zip_size = os.path.getsize(zp)
            char_id = name.replace("actor-cars3_", "")
            char_display = char_id.replace("_", " ").title()
            file_count = 0
            has_oct = has_vbuf = has_ibuf = has_textures = False
            try:
                zf = zipfile.ZipFile(zp, "r")
                file_count = len(zf.namelist())
                for fname in zf.namelist():
                    fl = fname.lower()
                    if fl.endswith(".oct"):
                        has_oct = True
                    elif "vbuf" in fl:
                        has_vbuf = True
                    elif "ibuf" in fl:
                        has_ibuf = True
                    elif fl.endswith(".tbody"):
                        has_textures = True
                zf.close()
            except Exception:
                pass
            export_dir = os.path.join(PROJECT_DIR, f"{char_id}_export")
            characters.append({
                "id": char_id,
                "name": char_display,
                "bundle": name,
                "path": f"romfs/bundles/{name}",
                "zipSize": zip_size,
                "fileCount": file_count,
                "hasOct": has_oct,
                "hasVbuf": has_vbuf,
                "hasIbuf": has_ibuf,
                "hasTextures": has_textures,
                "hasExport": os.path.isdir(export_dir),
                "canRender": has_oct and has_vbuf and has_ibuf,
            })
        self._json_response({"characters": characters, "count": len(characters)})

    def _handle_character_model(self, params):
        """Load full 3D model data for a character by ID."""
        char_id = params.get("id", [""])[0]
        if not char_id:
            self._json_response({"error": "No character id provided"}, 400)
            return
        bundles_dir = os.path.join(ROMFS_DIR, "bundles")
        bundle_name = f"actor-cars3_{char_id}"
        bundle_dir = os.path.join(bundles_dir, bundle_name)
        if not os.path.isdir(bundle_dir):
            self._json_response({"error": f"Character bundle not found: {char_id}"}, 404)
            return
        params["path"] = [f"romfs/bundles/{bundle_name}"]
        self._handle_model_view(params)

    # ════════════════════════════════════════════════════
    #  3D MODEL EXTRACTION  (core engine)
    # ════════════════════════════════════════════════════

    def _handle_model_view(self, params):
        """Parse OCT+VBUF+IBUF from a bundle and return geometry as JSON."""
        filepath = params.get("path", [""])[0]
        resolved, _ = self._resolve_path(filepath)
        if not resolved or not os.path.exists(resolved):
            self._json_response({"error": "Path not found"}, 404)
            return
        if not HAS_REVOCTANE:
            self._json_response({"error": "RevOctane not available"}, 500)
            return
        try:
            files_data = self._load_bundle_files(resolved)
            if not files_data:
                self._json_response({"error": "No data files found"}, 404)
                return

            oct_obj, oct_raw = self._find_oct_object(files_data)
            if not oct_obj:
                self._json_response({"error": "No parseable OCT found"}, 404)
                return

            vbuf_data = self._find_buffer(oct_obj, "VertexBufferPool", "FileName", files_data)
            ibuf_data = self._find_buffer(oct_obj, "IndexBufferPool", "FileName", files_data)
            if not vbuf_data or not ibuf_data:
                self._json_response({"error": "VBUF or IBUF not found"}, 404)
                return

            ibuf_u16 = struct.unpack(f"<{len(ibuf_data)//2}H", ibuf_data)
            mesh_node = self._find_best_mesh_node(oct_obj)
            if not mesh_node:
                self._json_response({"error": "No mesh node found"}, 404)
                return

            aabb = self._extract_aabb(mesh_node, oct_raw)
            result = self._extract_geometry(mesh_node, vbuf_data, ibuf_u16, aabb)
            if not result.get("vertexCount", 0):
                self._json_response({"error": "No vertices extracted"}, 404)
                return

            result["success"] = True
            self._json_response(result)
        except Exception as e:
            self._json_response({"error": str(e), "traceback": traceback.format_exc()}, 500)

    def _load_bundle_files(self, path):
        """Load all files from a bundle (directory with zip, zip file, or OCT directory)."""
        files_data = {}
        if os.path.isdir(path):
            for fname in os.listdir(path):
                if fname.endswith(".zip"):
                    zp = os.path.join(path, fname)
                    try:
                        zf = zipfile.ZipFile(zp, "r")
                        for name in zf.namelist():
                            files_data[name] = zf.read(name)
                        zf.close()
                    except Exception:
                        pass
                    break
            if not files_data:
                for fname in os.listdir(path):
                    fpath = os.path.join(path, fname)
                    if os.path.isfile(fpath) and not fname.startswith("._"):
                        with open(fpath, "rb") as f:
                            files_data[fname] = f.read()
        elif path.endswith(".zip"):
            zf = zipfile.ZipFile(path, "r")
            for name in zf.namelist():
                files_data[name] = zf.read(name)
            zf.close()
        elif path.endswith(".oct"):
            for fname in os.listdir(os.path.dirname(path)):
                fpath = os.path.join(os.path.dirname(path), fname)
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as f:
                        files_data[fname] = f.read()
                    if fname.endswith(".zip"):
                        try:
                            zf = zipfile.ZipFile(fpath, "r")
                            for zn in zf.namelist():
                                files_data[zn] = zf.read(zn)
                            zf.close()
                        except Exception:
                            pass
        return files_data

    def _find_oct_object(self, files_data):
        """Find and parse the main OCT file (skip motion files)."""
        oct_raw = None
        for k, v in files_data.items():
            if k.endswith(".oct") and "/motions/" not in k and "\\motions\\" not in k:
                if v[:4] == b"\x29\x76\x01\x45":
                    oct_raw = v
                    stream = bstream.BStream(stream=io.BytesIO(v))
                    try:
                        obj = revoctane.Octane(stream)
                        if obj._state != "fail" and hasattr(obj, "SceneTreeNodePool"):
                            return obj, oct_raw
                    except Exception:
                        pass
        return None, None

    def _find_buffer(self, oct_obj, pool_name, attr_name, files_data):
        """Find a buffer (VBUF/IBUF) referenced by the OCT object."""
        pool = getattr(oct_obj, pool_name, {})
        for idx, buf in pool.items():
            fn = getattr(buf, attr_name, None)
            if fn:
                for k, v in files_data.items():
                    if k.endswith(fn):
                        return v
        return None

    def _find_best_mesh_node(self, oct_obj):
        """Find the SceneTreeNode with the most vertices."""
        best_node = None
        best_verts = 0
        for idx, node in oct_obj.SceneTreeNodePool.items():
            if hasattr(node, "Primitives") and hasattr(node, "NumPrimitives") and node.NumPrimitives > 0:
                verts = sum(v.Vdata[1] for v in node.Primitives.values())
                if verts > best_verts:
                    best_verts = verts
                    best_node = node
        return best_node

    def _extract_aabb(self, mesh_node, oct_raw):
        """Extract bounding box from mesh node or brute-force scan."""
        aabb = None
        # Method 1: Node bounding box
        if hasattr(mesh_node, "BoundingBox"):
            bb = mesh_node["BoundingBox"]
            if len(bb) == 6:
                vals = [float(x) for x in bb]
                xM, yM, zM, xX, yX, zX = vals
                if xX > xM and yX > yM and zX > zM:
                    aabb = {"min": [xM, yM, zM], "max": [xX, yX, zX]}
        # Method 2: Brute-force scan
        if not aabb and oct_raw:
            candidates = self._scan_aabb(oct_raw)
            if candidates:
                candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
                vol, _, xM, yM, zM, xX, yX, zX = candidates[0]
                if xX - xM > 0.01:
                    aabb = {"min": [xM, yM, zM], "max": [xX, yX, zX]}
        # Method 3: Compute from raw vertex data as fallback
        if not aabb:
            aabb = self._compute_aabb_from_vertices(mesh_node)
        return aabb

    def _scan_aabb(self, raw):
        """Brute-force scan for float32 AABB candidates in binary data."""
        candidates = []
        fmt = struct.Struct("<6f")
        for i in range(0, len(raw) - 24, 4):
            try:
                vals = fmt.unpack_from(raw, i)
            except Exception:
                break
            xM, yM, zM, xX, yX, zX = vals
            if not (xX > xM and yX > yM and zX > zM):
                continue
            if any(abs(v) >= 50 for v in vals):
                continue
            vol = (xX - xM) * (yX - yM) * (zX - zM)
            if vol <= 0.01 or vol >= 500:
                continue
            dx, dy, dz = xX - xM, yX - yM, zX - zM
            maxD, minD = max(dx, dy, dz), min(dx, dy, dz)
            if minD <= 0 or maxD / minD >= 20:
                continue
            if all(abs(v) < 0.001 for v in vals):
                continue
            candidates.append((vol, abs(xM) + abs(yM) + abs(zM), xM, yM, zM, xX, yX, zX))
        return candidates

    def _compute_aabb_from_vertices(self, mesh_node):
        """Fallback: AABB from vertices is handled by the frontend via float32 detection."""
        return None

    def _extract_geometry(self, mesh_node, vbuf_data, ibuf_u16, aabb):
        """
        Extract geometry following the proven blender-export approach.
        
        Process ALL primitives in VBUF order, appending decoded vertices
        to a flat list. IBUF indices are used directly as vertex indices
        (they reference global VBUF positions which map 1:1 to the flat list
        when primitives are processed in order with non-overlapping ranges).
        
        Position: 3×uint16 (6 bytes) + UV: 2×uint16 (4 bytes) = 10 bytes
        Normals from second stream (vbeg2): 4×float16 (8 bytes)
        """

        # ── AABB for uint16→float32 decoding ──
        aabb_range = [1, 1, 1]
        aabb_min = [0, 0, 0]
        if aabb:
            for i in range(3):
                r = aabb["max"][i] - aabb["min"][i]
                if not r or r != r or abs(r) < 0.0001:
                    r = 1
                aabb_range[i] = r
                aabb_min[i] = aabb["min"][i] if aabb["min"][i] == aabb["min"][i] else 0

        # ── Step 1: decode ALL vertices from ALL primitives into flat lists ──
        all_positions = []
        all_normals = []
        all_uvs = []
        all_materials = []
        prim_infos = []  # For the primitives metadata

        # Sort primitives by their integer key (e.g. "0", "1", "2", ...)
        sorted_prims = sorted(mesh_node.Primitives.items(), key=lambda x: int(x[0]))

        for pidx, prim in sorted_prims:
            vdata = prim.Vdata
            vlen = vdata[1]
            vbeg = vdata[3]
            vstride = vdata[4]
            vbeg2 = vdata[6] if len(vdata) > 6 else 0
            vstride2 = vdata[7] if len(vdata) > 7 else 0
            mat_ref = getattr(prim, "MaterialReference", 0)
            idata = prim.Idata
            ipos = idata[1] // 2  # Idata[1] = byte offset into IBUF → uint16 offset
            ilen = idata[3]        # Idata[3] = uint16 index count

            start_vert = len(all_positions)

            for vi in range(vlen):
                off1 = vbeg + vi * vstride
                if off1 + 6 > len(vbuf_data):
                    all_positions.append([0.0, 0.0, 0.0])
                    all_uvs.append([0.0, 0.0])
                    all_normals.append([0.0, 1.0, 0.0])
                    all_materials.append(mat_ref)
                    continue

                # Position: 3 × uint16 → float32 via AABB decode
                x16, y16, z16 = struct.unpack_from("<3H", vbuf_data, off1)
                x = aabb_min[0] + (x16 / 65535.0) * aabb_range[0]
                y = aabb_min[1] + (y16 / 65535.0) * aabb_range[1]
                z = aabb_min[2] + (z16 / 65535.0) * aabb_range[2]
                all_positions.append([round(x, 6), round(y, 6), round(z, 6)])

                # UV: 2 × uint16 at offset 6
                if vstride >= 10:
                    u16, v16 = struct.unpack_from("<2H", vbuf_data, off1 + 6)
                    all_uvs.append([round(u16 / 65535.0, 6), round(1.0 - v16 / 65535.0, 6)])
                else:
                    all_uvs.append([0.0, 0.0])

                # Normal: from second stream as 4 × float16
                if vstride2 > 0 and vbeg2 > 0:
                    off2 = vbeg2 + vi * vstride2
                    if off2 + 8 <= len(vbuf_data):
                        vals = struct.unpack_from("<4e", vbuf_data, off2)
                        nx, ny, nz = vals[0], vals[1], vals[2]
                        ln = math.sqrt(nx*nx + ny*ny + nz*nz)
                        if ln > 0:
                            all_normals.append([nx/ln, ny/ln, nz/ln])
                        else:
                            all_normals.append([0.0, 1.0, 0.0])
                    else:
                        all_normals.append([0.0, 1.0, 0.0])
                else:
                    all_normals.append([0.0, 1.0, 0.0])

                all_materials.append(mat_ref)

            prim_infos.append({
                "idx": int(pidx),
                "vertexCount": vlen,
                "triangleCount": ilen // 3,
                "materialRef": mat_ref,
            })

        # ── Step 2: emit ALL indices (ibuf indices used directly as vertex indices) ──
        all_indices = []
        for pidx, prim in sorted_prims:
            idata = prim.Idata
            ipos = idata[1] // 2
            ilen = idata[3]
            for i in range(0, ilen, 3):
                if ipos + i + 2 >= len(ibuf_u16):
                    break
                i0 = ibuf_u16[ipos + i]
                i1 = ibuf_u16[ipos + i + 1]
                i2 = ibuf_u16[ipos + i + 2]
                # IBUF indices are global VBUF vertex indices.
                # Since primitives are processed in VBUF order with non-overlapping ranges,
                # the global index equals the sequential position in our flat list.
                # But guard against out-of-range just in case.
                if i0 >= len(all_positions) or i1 >= len(all_positions) or i2 >= len(all_positions):
                    continue
                # Emit all triangles (matching blender export approach)
                all_indices.extend([i0, i1, i2])

        total_verts = len(all_positions)
        result = {
            "primitives": prim_infos,
            "vertexCount": total_verts,
            "indexCount": len(all_indices),
            "positions": all_positions,
            "normals": all_normals,
            "uvs": all_uvs,
            "materials": all_materials,
            "indices": all_indices,
            "aabb": aabb,
            "trimmed": False,
        }

        MAX_VERTS = 150000
        if total_verts > MAX_VERTS:
            result["positions"] = all_positions[:MAX_VERTS]
            result["normals"] = all_normals[:MAX_VERTS]
            result["uvs"] = all_uvs[:MAX_VERTS]
            result["materials"] = all_materials[:MAX_VERTS]
            # Filter indices to only those within range
            result["indices"] = [i for i in all_indices if i < MAX_VERTS]
            result["vertexCount"] = MAX_VERTS
            result["indexCount"] = len(result["indices"])
            result["trimmed"] = True

        return result

    def _handle_extract_bundle(self, params, body):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, 400)
            return
        bundle_path = payload.get("bundle", "")
        file_in_zip = payload.get("file", "")
        bundles_dir = os.path.join(ROMFS_DIR, "bundles", bundle_path)
        if not os.path.isdir(bundles_dir):
            self._json_response({"error": "Bundle not found"}, 404)
            return
        zp = os.path.join(bundles_dir, "_root_.zip")
        if not os.path.exists(zp):
            zips = [f for f in os.listdir(bundles_dir) if f.endswith(".zip")]
            if not zips:
                self._json_response({"error": "No zip found"}, 404)
                return
            zp = os.path.join(bundles_dir, zips[0])
        try:
            zf = zipfile.ZipFile(zp, "r")
            data = zf.read(file_in_zip)
            zf.close()
            self._json_response({"data": base64.b64encode(data).decode(), "size": len(data), "encoding": "base64"})
        except KeyError:
            self._json_response({"error": f"File '{file_in_zip}' not found in bundle"}, 404)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    # ── Frontend serving ───────────────────────────────

    def _serve_frontend(self):
        html_path = os.path.join(PROJECT_DIR, "index.html")
        if not os.path.isfile(html_path):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<h1>404 - index.html not found</h1>")
            return
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(html_path, "rb") as f:
            self.wfile.write(f.read())

    def _serve_static(self, path):
        full = os.path.join(PROJECT_DIR, path.lstrip("/"))
        if not os.path.isfile(full):
            self._json_response({"error": "Not found"}, 404)
            return
        mime = self._guess_mime(path)
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "max-age=30")
        self.end_headers()
        with open(full, "rb") as f:
            self.wfile.write(f.read())

    def log_message(self, format, *args):
        if "127.0.0.1" in str(args[0]):
            return
        super().log_message(format, *args)


# ════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    banner = f"""
╔════════════════════════════════════════════════════════╗
║     Cars 3: Driven to Win — Ultimate Live Editor      ║
╠════════════════════════════════════════════════════════╣
║  ● Server:  http://127.0.0.1:{PORT}                       ║
║  ● RevOctane: {'✓ Loaded' if HAS_REVOCTANE else '✗ Missing'}                     ║
║  ● PIL:      {'✓ Loaded' if HAS_PIL else '✗ Missing'}                      ║
║  ● ExeFS:    {'✓ Found' if os.path.isdir(EXEFS_DIR) else '✗ Not found'}                  ║
║  ● RomFS:    {'✓ Found' if os.path.isdir(ROMFS_DIR) else '✗ Not found'}                  ║
║  ● Characters: ✓ auto-detecting from bundles          ║
╠════════════════════════════════════════════════════════╣
║  Open your browser to http://127.0.0.1:{PORT}            ║
║  Press Ctrl+C to stop.                                ║
╚════════════════════════════════════════════════════════╝
"""
    print(banner)

    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("", PORT), Cars3APIHandler)
        print(f"  Server listening on port {PORT}...")
        httpd.serve_forever()
    except OSError as e:
        print(f"  ✗ Failed to start server: {e}")
        print(f"  Make sure port {PORT} is not in use.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Server stopped.\n")
