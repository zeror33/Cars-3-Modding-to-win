#!/usr/bin/env python3
"""
Cars 3 romfs explorer — model viewer + mod maker + Ryujinx mod builder.
Serves OCT/VBUF/IBUF geometry, textures, and any file from romfs.
Real-time script editing, texture re-encoding, and Ryujinx romfs/exefs export.
"""
import os, sys, json, base64, struct, re, traceback, zipfile, mimetypes, hashlib
import io, shutil, subprocess, tempfile, math, threading

REVDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'RevOctane')
sys.path.insert(0, REVDIR)
sys.path.insert(0, '.')

import world_loader
from PIL import Image
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote
import bstream
from revoctane import Octane

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROMFS_DIR = os.path.join(BASE_DIR, 'romfs')
EXEFS_DIR = os.path.join(BASE_DIR, 'exefs')
CHARACTERS_DIR = os.path.join(ROMFS_DIR, 'assets', 'characters')
MOD_WORKSPACE = os.path.join(BASE_DIR, '.mod_workspace')
PORT = 8766

os.makedirs(MOD_WORKSPACE, exist_ok=True)

EXCLUDED_CHARS = {'animtree_includes', 'car', 'cars'}

ASSET_CATEGORIES = {
    'characters': 'Characters',
    'basicshapes': 'Basic Shapes',
    'choreographies': 'Choreographies',
    'env_assets': 'Environment',
    'exploders': 'Exploders',
    'objects': 'Objects',
    'realms': 'Realms',
    'weapons': 'Weapons',
    'worlds': 'Worlds',
}

EXCLUDED_ASSET_DIRS = {'excel', 'fonts', 'gamedb', 'lang', 'langselectbtn',
    'langselectnames', 'materials', 'meridianluanodes', 'panel_destruction',
    'panels_data', 'particles', 'scripts', 'shaders', 'sound',
    'spriteanimations', 'statuseffects', 'textures', 'ui', 'var',
    'expendables_data', 'fmv'}

def list_assets():
    """Scan romfs/assets/ for all directories containing model ZIPs."""
    categories = {}
    if not os.path.isdir(ROMFS_DIR):
        return categories
    assets_dir = os.path.join(ROMFS_DIR, 'assets')
    if not os.path.isdir(assets_dir):
        return categories
    for entry in sorted(os.listdir(assets_dir)):
        if entry.startswith('.') or entry in EXCLUDED_ASSET_DIRS:
            continue
        full = os.path.join(assets_dir, entry)
        if not os.path.isdir(full):
            continue
        items = []
        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if f.endswith('.zip') and not f.startswith('._'):
                    fpath = os.path.join(root, f)
                    rel = os.path.relpath(fpath, assets_dir)
                    name = f.replace('.zip', '')
                    # Check if it contains model data
                    has_model = False
                    has_geom = False
                    try:
                        with zipfile.ZipFile(fpath, 'r') as z:
                            for n in z.namelist():
                                if n.lower().endswith('.oct'):
                                    has_model = True
                                if n.lower().endswith('.vbuf'):
                                    has_geom = True
                    except Exception:
                        pass
                    if has_model and has_geom:
                        items.append({
                            'id': rel.replace('.zip', ''),
                            'name': name,
                            'path': rel,
                            'subdir': os.path.relpath(root, full) if root != full else '',
                        })
        if items:
            cat_name = ASSET_CATEGORIES.get(entry, entry.replace('_', ' ').title())
            categories[cat_name] = items[:200]  # limit per category
    return categories

# ── ZIP loading ──

def load_zip(path):
    data = {}
    with zipfile.ZipFile(path, 'r') as z:
        for name in z.namelist():
            try:
                data[name] = z.read(name)
            except Exception:
                pass
    return data

def _deswizzle_gob_offset(x, y):
    return (x // 32) * 256 + (y // 2) * 64 + ((x % 32) // 16) * 32 + (y & 1) * 16 + (x & 15)

def deswizzle_surface(data, width, height, block_size):
    """Deswizzle Nintendo Switch Tegra X1 block-linear (GOB) texture data to linear layout."""
    bx = (width + 3) // 4
    by = (height + 3) // 4
    total_bytes = bx * by * block_size
    out = bytearray(total_bytes)
    gob_h, gob_w = 8, 64
    gob_size = gob_w * gob_h
    bpg = gob_w // block_size
    gx = (bx + bpg - 1) // bpg
    gy = (by + gob_h - 1) // gob_h
    for gob_y in range(gy):
        for gob_x in range(gx):
            gob_idx = gob_y * gx + gob_x
            s_base = gob_idx * gob_size
            l_base_y = (gob_y * gob_h) * bx * block_size
            l_base_x = gob_x * bpg * block_size
            for row in range(gob_h):
                for col in range(gob_w):
                    sx = s_base + _deswizzle_gob_offset(col, row)
                    block_in_row = col // block_size
                    offset_in_block = col % block_size
                    block_y = gob_y * gob_h + row
                    block_x = gob_x * bpg + block_in_row
                    if block_x < bx and block_y < by:
                        lx = l_base_y + block_y * bx * block_size + l_base_x + block_in_row * block_size + offset_in_block
                        if sx < len(data) and lx < total_bytes:
                            out[lx] = data[sx]
    return bytes(out)

def deswizzle_block_linear(width, height, bytes_per_block, data):
    """
    Deswizzle Switch Tegra block-linear texture data using 16-GOB tile height.
    Works on BC-compressed blocks (BC1=8, BC3/BC7=16 bytes per block).
    """
    gob_width = 64
    gob_height = 8
    block_height_in_gobs = 16  # Default Switch tile height
    gob_size = gob_width * gob_height
    blocks_per_row = (width + 3) // 4
    bytes_per_row = blocks_per_row * bytes_per_block
    width_in_gobs = (bytes_per_row + gob_width - 1) // gob_width
    height_in_gobs = ((height + 3) // 4 + gob_height - 1) // gob_height
    out = bytearray(len(data))
    for gob_y in range(height_in_gobs):
        for gob_x in range(width_in_gobs):
            gob_idx = (gob_y // block_height_in_gobs) * width_in_gobs * block_height_in_gobs + \
                      gob_x * block_height_in_gobs + (gob_y % block_height_in_gobs)
            gob_offset = gob_idx * gob_size
            for line in range(gob_height):
                block_y = gob_y * gob_height + line
                if block_y * 4 >= height:
                    continue
                for bx in range(0, gob_width, bytes_per_block):
                    block_x = (gob_x * gob_width + bx) // bytes_per_block
                    if block_x >= blocks_per_row:
                        continue
                    src_idx = gob_offset + (line * gob_width) + bx
                    dst_idx = (block_y * blocks_per_row + block_x) * bytes_per_block
                    if src_idx + bytes_per_block <= len(data) and dst_idx + bytes_per_block <= len(out):
                        out[dst_idx:dst_idx+bytes_per_block] = data[src_idx:src_idx+bytes_per_block]
    return bytes(out)

def decode_texture_raw(raw_data, width, height, fmt, deswizzle=False):
    """Decode BC1/BC3/BC4/RGBA8 compressed texture data to raw RGBA bytes.
    ZIP tbody data is already in linear order (keep deswizzle=False for those).
    Pass deswizzle=True for raw Switch ROM textures in GOB-swizzled layout.
    The frontend JS decoder has a user-toggleable deswizzle option."""
    if deswizzle and fmt not in ('RGBA8', 'BC7') and width > 0 and height > 0 and len(raw_data) > 16:
        block_size = 8 if fmt in ('BC1', 'BC4') else 16
        # Only attempt deswizzle if data is large enough to be swizzled
        if len(raw_data) >= 64:
            raw_data = deswizzle_surface(raw_data, width, height, block_size)
    pixels = bytearray(width * height * 4)
    if fmt == 'BC3':
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                block_idx = (by // 4) * ((width + 3) // 4) + (bx // 4)
                boff = block_idx * 16
                if boff + 16 > len(raw_data): continue
                block = raw_data[boff:boff+16]
                a0, a1 = block[0], block[1]
                a_bits = int.from_bytes(block[2:8], 'little')
                c0 = struct.unpack_from('<H', block, 8)[0]; c1 = struct.unpack_from('<H', block, 10)[0]
                c_bits = struct.unpack_from('<I', block, 12)[0]
                r0 = ((c0>>11)&0x1F)*255//31; g0 = ((c0>>5)&0x3F)*255//63; b0 = (c0&0x1F)*255//31
                r1 = ((c1>>11)&0x1F)*255//31; g1 = ((c1>>5)&0x3F)*255//63; b1 = (c1&0x1F)*255//31
                alphas = [a0,(6*a0+1*a1)//7,(5*a0+2*a1)//7,(4*a0+3*a1)//7,(3*a0+4*a1)//7,(2*a0+5*a1)//7,(1*a0+6*a1)//7,a1] if a0>a1 else [a0,(4*a0+1*a1)//5,(3*a0+2*a1)//5,(2*a0+3*a1)//5,(1*a0+4*a1)//5,0,255,a1]
                colors = [(r0,g0,b0),((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3),((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3),(r1,g1,b1)] if c0>c1 else [(r0,g0,b0),((r0+r1)//2,(g0+g1)//2,(b0+b1)//2),(r1,g1,b1),(0,0,0)]
                for py in range(4):
                    for px in range(4):
                        x, y = bx + px, by + py
                        if x >= width or y >= height: continue
                        pi = (y*width+x)*4
                        ai = (a_bits>>((py*4+px)*3))&0x7; ci = (c_bits>>((py*4+px)*2))&0x3
                        pixels[pi+3]=alphas[ai]; pixels[pi:pi+3]=colors[ci]
    elif fmt == 'BC1':
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                block_idx = (by//4)*((width+3)//4)+(bx//4)
                boff = block_idx*8
                if boff+8>len(raw_data): continue
                block = raw_data[boff:boff+8]
                c0=struct.unpack_from('<H',block,0)[0]; c1=struct.unpack_from('<H',block,2)[0]
                bits=struct.unpack_from('<I',block,4)[0]
                r0=((c0>>11)&0x1F)*255//31; g0=((c0>>5)&0x3F)*255//63; b0=(c0&0x1F)*255//31
                r1=((c1>>11)&0x1F)*255//31; g1=((c1>>5)&0x3F)*255//63; b1=(c1&0x1F)*255//31
                if c0>c1:
                    colors=[(r0,g0,b0),((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3),((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3),(r1,g1,b1)]
                else:
                    colors=[(r0,g0,b0),((r0+r1)//2,(g0+g1)//2,(b0+b1)//2),(r1,g1,b1),(0,0,0)]
                for py in range(4):
                    for px in range(4):
                        x,y = bx+px, by+py
                        if x>=width or y>=height: continue
                        pi = (y*width+x)*4
                        ci = (bits>>((py*4+px)*2))&0x3
                        pixels[pi:pi+3]=colors[ci]; pixels[pi+3]=0 if (c0<=c1 and ci==3) else 255
    elif fmt == 'BC4':
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                block_idx = (by//4)*((width+3)//4)+(bx//4)
                boff = block_idx*8
                if boff+8>len(raw_data): continue
                block = raw_data[boff:boff+8]
                r0, r1 = block[0], block[1]
                r_bits = int.from_bytes(block[2:8], 'little')
                reds = [r0,(6*r0+1*r1)//7,(5*r0+2*r1)//7,(4*r0+3*r1)//7,(3*r0+4*r1)//7,(2*r0+5*r1)//7,(1*r0+6*r1)//7,r1] if r0>r1 else [r0,(4*r0+1*r1)//5,(3*r0+2*r1)//5,(2*r0+3*r1)//5,(1*r0+4*r1)//5,0,255,r1]
                for py in range(4):
                    for px in range(4):
                        x,y = bx+px, by+py
                        if x>=width or y>=height: continue
                        pi = (y*width+x)*4
                        ri = (r_bits>>((py*4+px)*3))&0x7
                        v = reds[ri]
                        pixels[pi]=v; pixels[pi+1]=v; pixels[pi+2]=v; pixels[pi+3]=255
    elif fmt == 'RGBA8':
        return raw_data[:width*height*4]
    elif fmt == 'BC5':
        import math
        for by in range(0, height, 4):
            for bx in range(0, width, 4):
                block_idx = (by//4)*((width+3)//4)+(bx//4)
                boff = block_idx*16
                if boff+16>len(raw_data): continue
                block = raw_data[boff:boff+16]
                for ch, ch_off in enumerate([0, 8]):
                    r0, r1 = block[ch_off], block[ch_off+1]
                    r_bits = int.from_bytes(block[ch_off+2:ch_off+8], 'little')
                    vals = [r0,(6*r0+1*r1)//7,(5*r0+2*r1)//7,(4*r0+3*r1)//7,(3*r0+4*r1)//7,(2*r0+5*r1)//7,(1*r0+6*r1)//7,r1] if r0>r1 else [r0,(4*r0+1*r1)//5,(3*r0+2*r1)//5,(2*r0+3*r1)//5,(1*r0+4*r1)//5,0,255,r1]
                    for py in range(4):
                        for px in range(4):
                            x,y = bx+px, by+py
                            if x>=width or y>=height: continue
                            pi = (y*width+x)*4
                            vi = (r_bits>>((py*4+px)*3))&0x7
                            pixels[pi+ch] = vals[vi]
                for py in range(4):
                    for px in range(4):
                        x,y = bx+px, by+py
                        if x>=width or y>=height: continue
                        pi = (y*width+x)*4
                        nx = (pixels[pi] / 255.0) * 2.0 - 1.0
                        ny = (pixels[pi+1] / 255.0) * 2.0 - 1.0
                        nz = math.sqrt(max(0.0, 1.0 - nx*nx - ny*ny))
                        pixels[pi+2] = int((nz + 1.0) * 0.5 * 255)
                        pixels[pi+3] = 255
    else:
        for py in range(height):
            for px in range(width):
                pi = (py*width+px)*4; pixels[pi:pi+4] = [255,0,255,255]
    return bytes(pixels)


def decode_texture_to_png_b64(raw_data, width, height, fmt):
    """Decode compressed texture data to a base64-encoded PNG string, or None on failure."""
    if width <= 0 or height <= 0 or not raw_data:
        return None
    try:
        rgba = decode_texture_raw(raw_data, width, height, fmt)
        img = Image.frombytes('RGBA', (width, height), rgba)
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=False)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def rgb_to_565(r, g, b):
    return ((r * 31 + 127) // 255) << 11 | ((g * 63 + 127) // 255) << 5 | ((b * 31 + 127) // 255)

def encode_bc3_block(rgba_block):
    """Encode a 4x4 RGBA pixel block to a 16-byte BC3 block."""
    pixels = []
    for py in range(4):
        for px in range(4):
            i = (py * 4 + px) * 4
            pixels.append((rgba_block[i], rgba_block[i+1], rgba_block[i+2], rgba_block[i+3]))

    alphas = [p[3] for p in pixels]
    a0, a1 = min(alphas), max(alphas)

    def quantize_alpha_8(a0, a1):
        endpoints = [a0, a1]
        for i in range(1, 7):
            endpoints.append((a0 * (7 - i) + a1 * i + 3) // 7)
        return sorted(set(endpoints))[:8] if a0 != a1 else [a0] * 7 + [255]

    def quantize_alpha_6(a0, a1):
        endpoints = [a0, a1, 0, 255]
        for i in range(1, 5):
            endpoints.append((a0 * (5 - i) + a1 * i + 2) // 5)
        return sorted(set(endpoints))[:8]

    if a0 > a1:
        alpha_table = quantize_alpha_8(a0, a1)
    else:
        alpha_table = quantize_alpha_6(a0, a1)
        if len(alpha_table) < 8:
            alpha_table.extend([0] * (8 - len(alpha_table)))

    def find_closest_alpha(val):
        best, best_dist = 0, 999999
        for i, a in enumerate(alpha_table):
            d = abs(val - a)
            if d < best_dist:
                best, best_dist = i, d
        return best

    a_bits = 0
    for i, p in enumerate(pixels):
        idx = find_closest_alpha(p[3])
        a_bits |= (idx & 7) << (i * 3)

    colors_rgb = [(p[0], p[1], p[2]) for p in pixels]
    unique_colors = list(set(colors_rgb))
    if len(unique_colors) <= 2:
        c0_rgb, c1_rgb = unique_colors[0], unique_colors[-1]
    else:
        c0_rgb = tuple(sum(c[i] for c in colors_rgb) // len(colors_rgb) for i in range(3))
        c1_rgb = unique_colors[0]
        best_var = 0
        for ch in range(3):
            vals = [c[ch] for c in unique_colors]
            v = max(vals) - min(vals)
            if v > best_var:
                best_var = v
                c1_rgb = tuple(
                    max(0, min(255, c0_rgb[j] + (max(c[j] for c in unique_colors) - min(c[j] for c in unique_colors)) * (1 if j == ch else 0)))
                    for j in range(3)
                )

    c0_565 = rgb_to_565(*c0_rgb)
    c1_565 = rgb_to_565(*c1_rgb)
    r0 = ((c0_565 >> 11) & 0x1F) * 255 // 31
    g0 = ((c0_565 >> 5) & 0x3F) * 255 // 63
    b0 = (c0_565 & 0x1F) * 255 // 31
    r1 = ((c1_565 >> 11) & 0x1F) * 255 // 31
    g1 = ((c1_565 >> 5) & 0x3F) * 255 // 63
    b1 = (c1_565 & 0x1F) * 255 // 31

    if c0_565 > c1_565:
        color_table = [
            (r0, g0, b0), (r1, g1, b1),
            ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3),
            ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3),
        ]
    else:
        color_table = [
            (r0, g0, b0), (r1, g1, b1),
            ((r0+r1)//2, (g0+g1)//2, (b0+b1)//2),
            (0, 0, 0),
        ]

    def find_closest_color(r, g, b):
        best, best_dist = 0, 999999
        for i, (cr, cg, cb) in enumerate(color_table):
            d = (r-cr)**2 + (g-cg)**2 + (b-cb)**2
            if d < best_dist:
                best, best_dist = i, d
        return best

    c_bits = 0
    for i, p in enumerate(pixels):
        idx = find_closest_color(p[0], p[1], p[2])
        c_bits |= (idx & 3) << (i * 2)

    block = bytearray(16)
    block[0] = a0 & 0xFF
    block[1] = a1 & 0xFF
    block[2:8] = a_bits.to_bytes(6, 'little')
    block[8:10] = struct.pack('<H', c0_565)
    block[10:12] = struct.pack('<H', c1_565)
    block[12:16] = c_bits.to_bytes(4, 'little')
    return bytes(block)

def encode_bc3(rgba_data, width, height):
    """Encode raw RGBA pixel data to BC3 compressed format."""
    out = bytearray()
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            block = bytearray(64)
            for py in range(4):
                for px in range(4):
                    x, y = min(bx + px, width - 1), min(by + py, height - 1)
                    si = (y * width + x) * 4
                    di = (py * 4 + px) * 4
                    block[di:di+4] = rgba_data[si:si+4]
            out.extend(encode_bc3_block(bytes(block)))
    return bytes(out)

def encode_rgba8(rgba_data, width, height):
    return bytes(rgba_data[:width * height * 4])


def parse_mtb_textures_fixed(zip_data, mtb_data):
    """Fixed version of parse_mtb_textures that handles mipmapped textures correctly."""
    textures = []
    if len(mtb_data) < 0x28:
        return textures
    num_tex = struct.unpack_from('<I', mtb_data, 0x1C)[0]
    for i in range(num_tex):
        base_off = 0x28 + i * 16
        if base_off + 16 > len(mtb_data):
            break
        hash_hex = mtb_data[base_off:base_off+8].hex()
        raw_fmt = struct.unpack_from('<H', mtb_data, base_off+10)[0]
        b13 = mtb_data[base_off+13]
        slot = mtb_data[base_off+14]
        tbody_name = next((name for name in zip_data if name.endswith(f'{hash_hex}.tbody')), None)
        if not tbody_name:
            continue
        tbody_data = zip_data[tbody_name]
        if len(tbody_data) < 16:
            continue
        fmt_code = world_loader.TEXTURE_FORMATS.get(raw_fmt, 'BC3')
        block_size = 16 if fmt_code not in ('BC1', 'BC4') else 8

        width, height = None, None
        data_to_decode = tbody_data
        if b13 > 0:
            blocks_per_col = (b13 + 3) // 4
            total_blocks = len(tbody_data) // block_size
            if blocks_per_col > 0 and total_blocks % blocks_per_col == 0:
                blocks_per_row = total_blocks // blocks_per_col
                width = blocks_per_row * 4
                height = blocks_per_col * 4
            else:
                for w in [256, 128, 512, 64, 1024, 32, 2048, 16, 4]:
                    bw = (w + 3) // 4
                    base_blocks = bw * blocks_per_col
                    base_data = base_blocks * block_size
                    if base_data <= len(tbody_data) and base_data >= len(tbody_data) * 0.3:
                        width = w
                        height = blocks_per_col * 4
                        data_to_decode = tbody_data[:base_data]
                        break

        if width is None:
            width, height = world_loader.compute_texture_dimensions(len(tbody_data), block_size)
            if width is None:
                continue

        textures.append({
            'hash': hash_hex, 'width': width, 'height': height, 'size': len(tbody_data),
            'format': fmt_code, 'slot': slot, 'mtbIndex': i,
            'data': base64.b64encode(data_to_decode).decode(),
        })
    return textures


def lua_compile(source, filename='script.lua'):
    """Compile Lua source to 5.1 bytecode using luac51. Returns bytes or None."""
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.lua', delete=False) as f:
            f.write(source)
            src_path = f.name
        out_path = src_path + '.luac'
        result = subprocess.run(
            ['luac51', '-o', out_path, src_path],
            capture_output=True, text=True, timeout=10
        )
        os.unlink(src_path)
        if result.returncode != 0:
            return None, result.stderr
        with open(out_path, 'rb') as f:
            bytecode = f.read()
        os.unlink(out_path)
        return bytecode, None
    except FileNotFoundError:
        return None, 'luac51 not found'
    except Exception as e:
        return None, str(e)

LUA51_OPCODES = {
    0: 'MOVE', 1: 'LOADK', 2: 'LOADBOOL', 3: 'LOADNIL', 4: 'GETUPVAL',
    5: 'GETGLOBAL', 6: 'GETTABLE', 7: 'SETGLOBAL', 8: 'SETUPVAL', 9: 'SETTABLE',
    10: 'NEWTABLE', 11: 'SELF', 12: 'ADD', 13: 'SUB', 14: 'MUL', 15: 'DIV',
    16: 'MOD', 17: 'POW', 18: 'UNM', 19: 'NOT', 20: 'LEN', 21: 'CONCAT',
    22: 'JMP', 23: 'EQ', 24: 'LT', 25: 'LE', 26: 'TEST', 27: 'TESTSET',
    28: 'CALL', 29: 'TAILCALL', 30: 'RETURN', 31: 'FORLOOP', 32: 'FORPREP',
    33: 'TFORLOOP', 34: 'SETLIST', 35: 'CLOSE', 36: 'CLOSURE', 37: 'VARARG',
}

def disassemble_lua51(bytecode):
    """Disassemble Lua 5.1 bytecode into readable pseudo-code."""
    try:
        lines = []
        pos = 0
        if bytecode[:4] != b'\x1bLua':
            return None

        version = bytecode[4]
        lines.append(f'-- Lua 5.{version - 0x50} Bytecode Disassembly')
        lines.append('-- ' + '=' * 50)

        # Lua 5.1 binary chunk header (12 bytes):
        #   bytes 0-3:  signature "\x1bLua"
        #   byte 4:     version (0x51)
        #   byte 5:     format (0)
        #   byte 6:     endianness (1=LE, 0=BE)
        #   byte 7:     sizeof(int)
        #   byte 8:     sizeof(size_t)
        #   byte 9:     sizeof(Instruction)
        #   byte 10:    sizeof(lua_Number)
        #   byte 11:    padding (0)
        #   Function prototype starts at offset 12.
        if bytecode[5] != 0:
            return f'-- Unsupported format byte: {bytecode[5]}'

        is_le = bytecode[6] == 1
        byteorder = 'little' if is_le else 'big'
        int_size    = bytecode[7]
        size_t_size = bytecode[8]
        instr_size  = bytecode[9]
        num_size    = bytecode[10]
        pos = 12  # start of main function prototype

        def read_int():
            nonlocal pos
            val = int.from_bytes(bytecode[pos:pos+int_size], byteorder, signed=True)
            pos += int_size
            return val

        def read_size_t():
            nonlocal pos
            val = int.from_bytes(bytecode[pos:pos+size_t_size], byteorder)
            pos += size_t_size
            return val

        def read_lua_num():
            nonlocal pos
            import struct
            fmt = f'{"<" if is_le else ">"}f' if num_size == 4 else f'{"<" if is_le else ">"}d'
            val = struct.unpack_from(fmt, bytecode, pos)[0]
            pos += num_size
            return val

        def read_string():
            nonlocal pos
            size = read_size_t()
            if size == 0:
                return ''
            s = bytecode[pos:pos+size-1].decode('utf-8', errors='replace')
            pos += size
            return s

        def read_instruction():
            nonlocal pos
            val = int.from_bytes(bytecode[pos:pos+instr_size], byteorder)
            pos += instr_size
            return val

        def disassemble_function(depth=1, name='main'):
            nonlocal pos
            indent = '  ' * depth

            func_source  = read_string()
            line_defined  = read_int()
            last_line_def = read_int()
            num_upvalues  = bytecode[pos]; pos += 1
            num_params    = bytecode[pos]; pos += 1
            is_vararg     = bytecode[pos]; pos += 1
            max_stack     = bytecode[pos]; pos += 1

            num_code = read_int()
            code_instructions = []
            for _ in range(num_code):
                code_instructions.append(read_instruction())

            num_consts_local = read_int()
            constants = []
            for _ in range(num_consts_local):
                t = bytecode[pos]; pos += 1
                if t == 0:
                    constants.append(None)
                elif t == 1:
                    constants.append(bool(bytecode[pos]))
                    pos += 1
                elif t == 3:
                    constants.append(read_lua_num())
                elif t == 4:
                    constants.append(read_string())
                else:
                    constants.append(f'<type{t}>')

            num_protos_local = read_int()

            debug_name = func_source if func_source else name
            if line_defined > 0:
                lines.append(f'{indent}-- function {debug_name} (line {line_defined})')
            else:
                lines.append(f'{indent}-- {debug_name} (top-level)')
            lines.append(f'{indent}-- params: {num_params}, upvalues: {num_upvalues}, vararg: {is_vararg}')

            i = 0
            for instr in code_instructions:
                # Lua 5.1 instruction format (32 bits):
                #   bits 0-5:   opcode (6 bits)
                #   bits 6-13:  C (8 bits)
                #   bits 14-22: B (9 bits)
                #   bits 23-31: A (9 bits)
                op = instr & 0x3F
                a = (instr >> 23) & 0x1FF
                b = (instr >> 14) & 0x1FF
                c = (instr >> 6) & 0x1FF
                bx = (instr >> 6) & 0x3FFFF
                sbx = bx - 0x10000

                opname = LUA51_OPCODES.get(op, f'OP_{op}')

                if op == 0:    # MOVE
                    lines.append(f'{indent}  {i}: R{a} = R{b}')
                elif op == 1:  # LOADK
                    cv = constants[bx] if bx < len(constants) else '?'
                    lines.append(f'{indent}  {i}: R{a} = {repr(cv)}')
                elif op == 2:  # LOADBOOL
                    lines.append(f'{indent}  {i}: R{a} = {bool(b)}')
                elif op == 3:  # LOADNIL
                    lines.append(f'{indent}  {i}: R{a} = nil')
                elif op == 4:  # GETUPVAL
                    lines.append(f'{indent}  {i}: R{a} = upval[{b}]')
                elif op == 5:  # GETGLOBAL
                    cv = constants[bx] if bx < len(constants) else '?'
                    lines.append(f'{indent}  {i}: R{a} = {cv}')
                elif op == 6:  # GETTABLE
                    lines.append(f'{indent}  {i}: R{a} = R{b}[R{c}]')
                elif op == 7:  # SETGLOBAL
                    cv = constants[bx] if bx < len(constants) else '?'
                    lines.append(f'{indent}  {i}: {cv} = R{a}')
                elif op == 8:  # SETUPVAL
                    lines.append(f'{indent}  {i}: upval[{b}] = R{a}')
                elif op == 9:  # SETTABLE
                    lines.append(f'{indent}  {i}: R{b}[R{c}] = R{a}')
                elif op == 10: # NEWTABLE
                    lines.append(f'{indent}  {i}: R{a} = {{}}')
                elif op == 11: # SELF
                    lines.append(f'{indent}  {i}: R{a+1} = R{b}[R{c}]; R{a} = R{b}')
                elif op == 12: # ADD
                    lines.append(f'{indent}  {i}: R{a} = R{b} + R{c}')
                elif op == 13: # SUB
                    lines.append(f'{indent}  {i}: R{a} = R{b} - R{c}')
                elif op == 14: # MUL
                    lines.append(f'{indent}  {i}: R{a} = R{b} * R{c}')
                elif op == 15: # DIV
                    lines.append(f'{indent}  {i}: R{a} = R{b} / R{c}')
                elif op == 16: # MOD
                    lines.append(f'{indent}  {i}: R{a} = R{b} % R{c}')
                elif op == 17: # POW
                    lines.append(f'{indent}  {i}: R{a} = R{b} ^ R{c}')
                elif op == 18: # UNM
                    lines.append(f'{indent}  {i}: R{a} = -R{b}')
                elif op == 19: # NOT
                    lines.append(f'{indent}  {i}: R{a} = not R{b}')
                elif op == 20: # LEN
                    lines.append(f'{indent}  {i}: R{a} = #R{b}')
                elif op == 21: # CONCAT
                    parts = [f'R{b+j}' for j in range(c - b + 1)]
                    lines.append(f'{indent}  {i}: R{a} = {"..".join(parts)}')
                elif op == 22: # JMP
                    lines.append(f'{indent}  {i}: jmp +{sbx}')
                elif op == 23: # EQ
                    lines.append(f'{indent}  {i}: if R{b} == R{c} then goto {i+1+sbx}')
                elif op == 24: # LT
                    lines.append(f'{indent}  {i}: if R{b} < R{c} then goto {i+1+sbx}')
                elif op == 25: # LE
                    lines.append(f'{indent}  {i}: if R{b} <= R{c} then goto {i+1+sbx}')
                elif op == 26: # TEST
                    lines.append(f'{indent}  {i}: if R{a} == {bool(c)} then goto {i+1+sbx}')
                elif op == 27: # TESTSET
                    lines.append(f'{indent}  {i}: R{a} = R{b}; if R{a} then goto {i+1+sbx}')
                elif op == 28: # CALL
                    args = ', '.join(f'R{b+j}' for j in range(c - 1)) if c > 1 else ''
                    lines.append(f'{indent}  {i}: R{a}({args})')
                elif op == 29: # TAILCALL
                    args = ', '.join(f'R{b+j}' for j in range(c - 1)) if c > 1 else ''
                    lines.append(f'{indent}  {i}: return R{a}({args})')
                elif op == 30: # RETURN
                    if c == 0:
                        lines.append(f'{indent}  {i}: return R{a}, ...')
                    elif c == 1:
                        lines.append(f'{indent}  {i}: return')
                    else:
                        parts = ', '.join(f'R{a+j}' for j in range(c - 1))
                        lines.append(f'{indent}  {i}: return {parts}')
                elif op == 31: # FORLOOP
                    lines.append(f'{indent}  {i}: R{a} = R{a}+R{a+2}; if R{a} <= R{a+1} then R{a+3} = R{a}; goto {i+1+sbx}')
                elif op == 32: # FORPREP
                    lines.append(f'{indent}  {i}: R{a} = R{a}-R{a+2}; goto {i+1+sbx}')
                elif op == 33: # TFORLOOP
                    args = ', '.join(f'R{a+3+j}' for j in range(b))
                    lines.append(f'{indent}  {i}: {args} = R{a}(R{a+1}, R{a+2}); if R{a+3} then goto {i+1}')
                elif op == 34: # SETLIST
                    lines.append(f'{indent}  {i}: setlist(R{a}, {c}, {b if c > 0 else "multret"})')
                elif op == 35: # CLOSE
                    lines.append(f'{indent}  {i}: close R{a}')
                elif op == 36: # CLOSURE
                    lines.append(f'{indent}  {i}: R{a} = closure(func#{bx})')
                elif op == 37: # VARARG
                    if c == 0:
                        lines.append(f'{indent}  {i}: R{a} = ...')
                    else:
                        parts = ', '.join(f'R{a+j}' for j in range(c))
                        lines.append(f'{indent}  {i}: {parts} = ...')
                else:
                    lines.append(f'{indent}  {i}: {opname} R{a} {b} {c}')
                i += 1

            lines.append('')

            for pi in range(num_protos_local):
                disassemble_function(depth + 1, f'{debug_name}/sub#{pi}')

        disassemble_function()

        return '\n'.join(lines)
    except Exception as e:
        return f'-- Disassembly failed: {e}'

def lua_decompile_bytecode(bytecode):
    """Try to decompile Lua 5.1 bytecode to readable form."""
    if bytecode[:4] == b'\x1bLua':
        try:
            result = subprocess.run(
                ['luajit', '-bl', '-'],
                input=bytecode, capture_output=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.decode('utf-8', errors='replace')
        except Exception:
            pass
        disasm = disassemble_lua51(bytecode)
        if disasm:
            return disasm
    return None


# ── Mod workspace helpers ──

def mod_workspace_path(rel):
    """Resolve a path inside the mod workspace, preventing escapes."""
    rel = unquote(rel).lstrip('/')
    target = os.path.normpath(os.path.join(MOD_WORKSPACE, rel))
    if not target.startswith(MOD_WORKSPACE):
        return None
    return target

def mod_list_workspace():
    """List all files in the mod workspace."""
    items = []
    for root, dirs, files in os.walk(MOD_WORKSPACE):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, MOD_WORKSPACE)
            items.append({
                'path': rel,
                'size': os.path.getsize(full),
                'ext': os.path.splitext(f)[1].lower(),
            })
    return items

def mod_extract_from_zip(zip_path, inner_name, dest_rel):
    """Extract a file from a romfs ZIP to the mod workspace."""
    target = safe_path(zip_path)
    if not target or not os.path.isfile(target):
        return None, 'ZIP not found'
    dest = mod_workspace_path(dest_rel)
    if not dest:
        return None, 'Invalid destination'
    try:
        with zipfile.ZipFile(target, 'r') as z:
            raw = z.read(inner_name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(raw)
        return {'path': dest_rel, 'size': len(raw)}, None
    except Exception as e:
        return None, str(e)

def mod_save_file(rel, content_b64):
    """Save a base64-encoded file to the mod workspace."""
    dest = mod_workspace_path(rel)
    if not dest:
        return None, 'Invalid path'
    try:
        raw = base64.b64decode(content_b64)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(raw)
        return {'path': rel, 'size': len(raw)}, None
    except Exception as e:
        return None, str(e)

def mod_save_text(rel, text):
    """Save a text file to the mod workspace."""
    dest = mod_workspace_path(rel)
    if not dest:
        return None, 'Invalid path'
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'w') as f:
            f.write(text)
        return {'path': rel, 'size': len(text.encode())}, None
    except Exception as e:
        return None, str(e)

def mod_repack_into_zip(workspace_files, output_zip_path):
    """Repack modified files from workspace into a ZIP, preserving structure from original."""
    try:
        original_zip_path = safe_path(output_zip_path)
        if not original_zip_path or not os.path.isfile(original_zip_path):
            return None, 'Original ZIP not found'

        tmp_path = original_zip_path + '.mod_tmp'
        with zipfile.ZipFile(original_zip_path, 'r') as zin:
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    for wf in workspace_files:
                        if item.filename == wf.get('zip_inner', ''):
                            workspace_path = mod_workspace_path(wf['workspace_rel'])
                            if workspace_path and os.path.isfile(workspace_path):
                                with open(workspace_path, 'rb') as f:
                                    data = f.read()
                                break
                    zout.writestr(item, data)

        shutil.move(tmp_path, original_zip_path)
        return {'repacked': output_zip_path, 'files_modified': len(workspace_files)}, None
    except Exception as e:
        return None, str(e)

def mod_export_ryujinx(mod_name='cars3_mod'):
    """Export mod workspace as Ryujinx mod structure (romfs/ + exefs/)."""
    export_dir = os.path.join(BASE_DIR, f'.ryujinx_export_{mod_name}')
    romfs_out = os.path.join(export_dir, 'romfs')
    exefs_out = os.path.join(export_dir, 'exefs')

    try:
        os.makedirs(romfs_out, exist_ok=True)
        os.makedirs(exefs_out, exist_ok=True)

        for root, dirs, files in os.walk(MOD_WORKSPACE):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if f.startswith('.'):
                    continue
                src = os.path.join(root, f)
                rel = os.path.relpath(src, MOD_WORKSPACE)
                if rel.startswith('romfs_replacement' + os.sep) or rel.startswith('romfs_replacement/'):
                    romfs_rel = rel[len('romfs_replacement') + 1:]
                    dst = os.path.join(romfs_out, romfs_rel)
                else:
                    dst = os.path.join(romfs_out, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

        if os.path.isdir(EXEFS_DIR):
            for root, dirs, files in os.walk(EXEFS_DIR):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    if f.startswith('.'):
                        continue
                    src = os.path.join(root, f)
                    rel = os.path.relpath(src, EXEFS_DIR)
                    dst = os.path.join(exefs_out, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)

        mod_meta = os.path.join(export_dir, 'ryujinx_mod.toml')
        with open(mod_meta, 'w') as f:
            f.write(f'title_name = "Cars 3 Driven to Win"\n')
            f.write(f'title_id = "0100C2D00A5C2000"\n')
            f.write(f'[romfs]\n')
            f.write(f'enabled = true\n')
            f.write(f'[exefs]\n')
            f.write(f'enabled = {"true" if os.path.isdir(EXEFS_DIR) and os.listdir(exefs_out) else "false"}\n')

        return {'export_dir': export_dir, 'romfs_files': mod_list_workspace_recursive(romfs_out), 'exefs_files': mod_list_workspace_recursive(exefs_out)}, None
    except Exception as e:
        return None, str(e)

def mod_list_workspace_recursive(directory):
    items = []
    for root, dirs, files in os.walk(directory):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, directory)
            items.append({'path': rel, 'size': os.path.getsize(full)})
    return items


def parse_tbody(data, filename=''):
    """
    Parses standalone .tbody/.dds texture files into structured texture objects
    compatible with the frontend BCn decoder pipeline.
    """
    if len(data) < 0x20:
        return None

    tex_hash = os.path.splitext(os.path.basename(filename))[0].lower() if filename else ''

    if data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff'):
        b64 = base64.b64encode(data).decode()
        return {
            'hash': tex_hash,
            'width': 0, 'height': 0,
            'format': 'PNG',
            'size': len(data),
            'slot': 0, 'mtbIndex': 0,
            'data': b64,
            'png_data': b64,
        }

    dds_offset = data.find(b'DDS ')
    if dds_offset != -1:
        dds_data = data[dds_offset:]
        try:
            width = struct.unpack_from('<I', dds_data, 12)[0]
            height = struct.unpack_from('<I', dds_data, 8)[0]
            fourcc = dds_data[80:84]

            fmt_map = {
                b'DXT1': 'BC1',
                b'DXT3': 'BC3',
                b'DXT5': 'BC3',
                b'BC7 ': 'BC7',
            }
            fmt_code = fmt_map.get(fourcc, 'BC3')

            payload = dds_data[128:]

            return {
                'hash': tex_hash,
                'width': width, 'height': height,
                'format': fmt_code,
                'size': len(payload),
                'slot': 0, 'mtbIndex': 0,
                'data': base64.b64encode(payload).decode(),
            }
        except Exception:
            pass

    from world_loader import compute_texture_dimensions
    fmt_code = 'BC3'
    block_size = 16
    if data[:4] == b'DXT1':
        fmt_code = 'BC1'
        block_size = 8
    width, height = compute_texture_dimensions(len(data), block_size)
    if width is None:
        width, height = 256, 256

    return {
        'hash': tex_hash,
        'width': width, 'height': height,
        'format': fmt_code,
        'size': len(data),
        'slot': 0, 'mtbIndex': 0,
        'data': base64.b64encode(data).decode(),
    }

def extract_groups_from_zip(zip_data, break_after_first=False):
    """Process all .oct files in zip_data, aggregating geometry, materials, and textures."""
    groups = []
    tex_list = []
    mat_list = []
    mat_tex_map = {}
    seen_vbuf_hashes = set()
    seen_mat_names = set()
    seen_tex_hashes = set()

    oct_entries = [(n, r) for n, r in zip_data.items() if n.lower().endswith('.oct')]
    oct_entries.sort(key=lambda x: (0 if '/' not in x[0] else 1, -len(x[1])))

    filtered_oct = []
    for name, raw in oct_entries:
        name_lower = name.lower()
        if '/motions/' in name_lower:
            continue
        if any(name_lower.endswith(s) for s in ['_lod1.oct', '_lod2.oct', '_lod3.oct', '_dmg.oct']):
            continue
        filtered_oct.append((name, raw))
    oct_entries = filtered_oct

    for name, raw in oct_entries:
        try:
            stream = bstream.BStream(bytes=raw)
            obj = Octane(stream)
        except Exception:
            continue

        vpool = obj.get('VertexBufferPool', {})
        if not vpool:
            continue

        vbuf_fn = ibuf_fn = None
        for k in vpool:
            fn = vpool[k].get('FileName', '')
            if fn and fn in zip_data:
                vbuf_fn = fn
                break
        ipool = obj.get('IndexBufferPool', {})
        for k in ipool:
            fn = ipool[k].get('FileName', '')
            if fn and fn in zip_data:
                ibuf_fn = fn
                break

        vbuf_data = zip_data.get(vbuf_fn, b'') if vbuf_fn else b''
        vbuf_hash = hashlib.md5(vbuf_data).hexdigest()
        if vbuf_hash in seen_vbuf_hashes:
            continue
        seen_vbuf_hashes.add(vbuf_hash)

        group = extract_buffer_group(zip_data, obj)
        if group:
            groups.append(group)

        matpool = obj.get('MaterialPool', {})
        for k in sorted(matpool.keys(), key=lambda x: int(x)):
            m = matpool[k]
            m_name = m.get('Name', '')
            if m_name and m_name in seen_mat_names:
                continue
            mat_list.append({
                'index': int(k),
                'name': m_name,
                'fileName': m.get('FileName', ''),
            })
            if m_name:
                seen_mat_names.add(m_name)

        if break_after_first and groups:
            break

    for zname, data in zip_data.items():
        if zname.endswith('.mtb'):
            new_tex = parse_mtb_textures_fixed(zip_data, data)
            for t in new_tex:
                if t['hash'] not in seen_tex_hashes:
                    tex_list.append(t)
                    seen_tex_hashes.add(t['hash'])
            matp = world_loader.parse_matp(data)
            if matp:
                mat_tex_map.update(matp)

    for zname, data in zip_data.items():
        if zname.lower().endswith('.tbody') or zname.lower().endswith('.dds'):
            tex = parse_tbody(data, zname)
            if tex and tex['hash'] not in seen_tex_hashes:
                tex_list.append(tex)
                seen_tex_hashes.add(tex['hash'])

    # png_data is intentionally NOT generated here so the frontend deswizzle toggle works.
    # The JS decoder in viewer.html respects the toggle; pre-decoded server-side PNGs bypass it.
    return groups, tex_list, mat_list, mat_tex_map

# ── Character discovery ──

def list_characters():
    chars = []
    if not os.path.isdir(CHARACTERS_DIR):
        return chars
    for entry in sorted(os.listdir(CHARACTERS_DIR)):
        if entry.lower() in EXCLUDED_CHARS or entry.startswith('.'):
            continue
        full = os.path.join(CHARACTERS_DIR, entry)
        if not os.path.isdir(full):
            continue
        zip_files = [f for f in os.listdir(full) if f.endswith('.zip') and not f.startswith('._')]
        if zip_files:
            chars.append({'id': entry, 'name': entry.replace('cars3_', '').replace('_', ' ').title()})
    return chars

# ── Scene extraction ──

def find_geometry_nodes(obj):
    pool = obj.get('SceneTreeNodePool', {})
    nodes = []
    for k in sorted(pool.keys(), key=lambda x: int(x)):
        node = pool[k]
        if node.get('Type') == 'Geometry' and 'Primitives' in node:
            nodes.append((k, node))
    return nodes

def extract_buffer_group(zip_data, obj):
    vpool = obj.get('VertexBufferPool', {})
    ipool = obj.get('IndexBufferPool', {})
    ibuf_pool = obj.get('IndexBufferPool', {})

    vbuf = ibuf = None
    for k in vpool:
        entry = vpool[k]
        fn = entry.get('FileName', None)
        if fn and fn in zip_data:
            vbuf = zip_data[fn]
            break
    if vbuf is None:
        for name in zip_data:
            if name.lower().endswith('.vbuf'):
                vbuf = zip_data[name]
                break

    for k in ipool:
        entry = ipool[k]
        fn = entry.get('FileName', None)
        if fn and fn in zip_data:
            ibuf = zip_data[fn]
            break
    if ibuf is None:
        for name in zip_data:
            if name.lower().endswith('.ibuf'):
                ibuf = zip_data[name]
                break

    if not vbuf:
        return None

    matpool = obj.get('MaterialPool', {})
    mat_names = {}
    for k in sorted(matpool.keys(), key=lambda x: int(x)):
        mat_names[int(k)] = matpool[k].get('Name', matpool[k].get('FileName', ''))

    node_skip_keywords = ['lod1', 'lod2', 'lod3', '_lod', 'shadow', 'proxy', 'collision', '_dmg', 'damage', 'chassis_b', 'chassis_lod']

    def is_duplicate_node(node_name):
        name = node_name.lower()
        return any(kw in name for kw in node_skip_keywords)

    prims = []
    geom_nodes = find_geometry_nodes(obj)
    for node_key, node in geom_nodes:
        node_name = node.get('NodeName', node_key)
        if is_duplicate_node(node_name):
            continue
        node_prims = node.get('Primitives', {})
        for pk in sorted(node_prims.keys(), key=lambda x: int(x)):
            try:
                prim = node_prims[pk]
                mat_name = prim.get('MaterialName', 'unknown')
                if 'shadowcaster' in mat_name.lower():
                    continue

                vdata = [int(x) for x in prim.get('Vdata', [2, 0, 0, 0, 12, 0, 0, 8])]
                idata = [int(x) for x in prim.get('Idata', [0, 0, 0, 0])]

                ib_ref = idata[0]
                ib_entry = ibuf_pool.get(str(ib_ref))
                index_width = ib_entry.get('Width', 2) if ib_entry else 2

                vert_count = vdata[1]
                offset_a = vdata[3]
                stride_a = vdata[4]
                byte_end = offset_a + vert_count * stride_a
                if vbuf and byte_end > len(vbuf):
                    continue

                idx_count = idata[3]
                idx_byte_end = idata[1] + idx_count * index_width
                if ibuf and idx_byte_end > len(ibuf):
                    continue

                mat_ref = int(prim.get('MaterialReference', -1))
                prims.append({
                    'node': node_name,
                    'material': mat_name,
                    'materialRef': mat_ref,
                    'materialDisplayName': mat_names.get(mat_ref, mat_name),
                    'unitBase': [float(x) for x in prim.get('UnitBase', [0, 0, 0])],
                    'unitScale': [float(x) for x in prim.get('UnitScale', [1, 1, 1])],
                    'vdata': vdata,
                    'idata': idata,
                    'indexWidth': index_width,
                })
            except Exception:
                continue

    if not prims:
        return None

    return {
        'vbuf': base64.b64encode(vbuf).decode(),
        'ibuf': base64.b64encode(ibuf).decode() if ibuf else '',
        'primitives': prims,
    }

    # ... (existing imports)
# ...

# ── MTB texture parsing (moved to world_loader) ──
# ── Romfs browser ──

def extract_character_data(char_id):
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    if not os.path.isdir(char_dir):
        return None, f'Character directory not found: {char_id}'
    zip_files = [f for f in os.listdir(char_dir) if f.endswith('.zip') and not f.startswith('._')]
    if not zip_files:
        return None, 'No zip files found'

    groups = []
    tex_list = []
    mat_list = []
    mat_tex_map = {}

    for zf_name in zip_files:
        try:
            zip_data = load_zip(os.path.join(char_dir, zf_name))
        except Exception:
            continue
        g, t, m, mt = extract_groups_from_zip(zip_data, True)
        if g and not groups:
            groups = g
        tex_list.extend(t)
        mat_list.extend(m)
        mat_tex_map.update(mt)

    if not groups:
        return None, 'No renderable geometry found'

    return {
        'groups': groups,
        'textures': tex_list,
        'materials': mat_list,
        'matTexMap': mat_tex_map,
    }, None


def extract_asset_data(asset_path):
    """Load model data from any asset ZIP file in romfs/assets/."""
    target = safe_path('assets/' + asset_path + '.zip')
    if not target or not os.path.isfile(target):
        return None, 'Asset ZIP not found'

    try:
        zip_data = load_zip(target)
    except Exception as e:
        return None, f'Failed to load asset: {e}'

    groups, tex_list, mat_list, mat_tex_map = extract_groups_from_zip(zip_data)

    if not groups:
        return None, 'No renderable geometry found'

    return {
        'groups': groups,
        'textures': tex_list,
        'materials': mat_list,
        'matTexMap': mat_tex_map,
    }, None

# ── Romfs browser ──

def safe_path(rel):
    """Resolve a relative path inside ROMFS_DIR, preventing escapes."""
    rel = unquote(rel).lstrip('/')
    target = os.path.normpath(os.path.join(ROMFS_DIR, rel))
    if not target.startswith(ROMFS_DIR):
        return None
    return target

def browse_dir(rel_path=''):
    target = safe_path(rel_path)
    if not target or not os.path.isdir(target):
        return None
    entries = []
    try:
        for name in sorted(os.listdir(target)):
            if name.startswith('.'):
                continue
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            size = 0
            ext = ''
            if not is_dir:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    pass
                ext = os.path.splitext(name)[1].lower()
            entries.append({
                'name': name,
                'type': 'dir' if is_dir else 'file',
                'size': size,
                'ext': ext,
            })
    except PermissionError:
        return None
    return entries

def get_file_info(rel_path):
    target = safe_path(rel_path)
    if not target or not os.path.isfile(target):
        return None, None, None
    size = os.path.getsize(target)
    mime, _ = mimetypes.guess_type(target)
    ext = os.path.splitext(target)[1].lower()
    return target, size, ext

def scan_romfs_by_exts(extensions, max_results=200):
    items = []
    for root, dirs, files in os.walk(ROMFS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in extensions:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, ROMFS_DIR)
                items.append({
                    'name': f,
                    'path': rel,
                    'size': os.path.getsize(full),
                    'ext': ext,
                })
                if len(items) >= max_results:
                    return items
    return items

_SCRIPTS_CACHE = None
def scan_romfs_scripts(max_results=2000):
    global _SCRIPTS_CACHE
    if _SCRIPTS_CACHE is not None:
        return _SCRIPTS_CACHE
    exts = {'.lua', '.sx', '.js', '.xml', '.json', '.txt', '.cfg', '.ini', '.properties', '.manifest', '.as'}
    items = []
    for root, dirs, files in os.walk(ROMFS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            ext = os.path.splitext(f)[1].lower()
            full = os.path.join(root, f)
            rel = os.path.relpath(full, ROMFS_DIR)
            if ext == '.zip':
                try:
                    with zipfile.ZipFile(full, 'r') as z:
                        for info in z.infolist():
                            if info.is_dir():
                                continue
                            inner_ext = os.path.splitext(info.filename)[1].lower()
                            if inner_ext in exts:
                                items.append({
                                    'name': os.path.basename(info.filename),
                                    'path': rel + '//' + info.filename,
                                    'zipPath': rel,
                                    'innerPath': info.filename,
                                    'size': info.file_size,
                                    'ext': inner_ext,
                                })
                                if len(items) >= max_results:
                                    _SCRIPTS_CACHE = items
                                    return items
                except Exception:
                    pass
            elif ext in exts:
                items.append({
                    'name': f,
                    'path': rel,
                    'zipPath': None,
                    'innerPath': None,
                    'size': os.path.getsize(full),
                    'ext': ext,
                })
    _SCRIPTS_CACHE = items
    return items

# ── HTTP handler ──

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = parse_qs(parsed.query)

        try:
            if path == '' or path == '/' or path == '/index.html':
                self.send_file(os.path.join(BASE_DIR, 'viewer.html'), 'text/html')

            elif path.endswith('.js') or path.endswith('.css') or path.endswith('.png') or path.endswith('.svg') or path.endswith('.ico') or path.endswith('.jpg') or path.endswith('.gif') or path.endswith('.json'):
                local = os.path.join(BASE_DIR, path.lstrip('/'))
                if os.path.isfile(local):
                    ct = mimetypes.guess_type(path)[0] or 'application/octet-stream'
                    self.send_file(local, ct)
                else:
                    self.send_error(404)

            elif path == '/api/characters':
                self.send_json({'characters': list_characters()})

            elif path == '/api/assets':
                self.send_json({'categories': list_assets()})

            elif path == '/api/asset':
                asset_path = params.get('path', [None])[0]
                if not asset_path:
                    self.send_json({'error': 'Missing path parameter'}, 400)
                    return
                data, err = extract_asset_data(asset_path)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(data)

            elif path == '/api/character':
                char_id = params.get('id', [None])[0]
                if not char_id:
                    self.send_json({'error': 'Missing id parameter'}, 400)
                    return
                data, err = extract_character_data(char_id)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(data)

            elif path == '/api/browse':
                rel = params.get('path', [''])[0]
                entries = browse_dir(rel)
                if entries is None:
                    self.send_json({'error': 'Directory not found'}, 404)
                    return
                self.send_json({'path': rel, 'entries': entries})

            elif path == '/api/file':
                rel = params.get('path', [''])[0]
                target, size, ext = get_file_info(rel)
                if not target:
                    self.send_json({'error': 'File not found'}, 404)
                    return

                if size > 50 * 1024 * 1024:
                    self.send_json({'error': 'File too large (>50MB)'}, 400)
                    return

                is_text_ext = ext in ('.txt', '.lua', '.js', '.json', '.xml', '.cfg',
                    '.csv', '.ini', '.yaml', '.yml', '.toml', '.md', '.py', '.sh',
                    '.bat', '.html', '.css', '.htm', '.properties', '.manifest')
                is_audio = ext in ('.wem', '.bnk', '.pck')
                is_video = ext in('.bik',)
                is_image = ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tga')

                if is_text_ext or ext == '':
                    try:
                        with open(target, 'rb') as f:
                            raw = f.read()
                        text = raw.decode('utf-8', errors='replace')
                        self.send_json({
                            'type': 'text',
                            'name': os.path.basename(target),
                            'size': size,
                            'ext': ext,
                            'content': text,
                        })
                    except Exception as e:
                        self.send_json({'error': str(e)}, 500)

                elif is_audio:
                    try:
                        with open(target, 'rb') as f:
                            raw = f.read()
                        b64 = base64.b64encode(raw).decode()
                        audio_type = 'audio/ogg' if ext == '.wem' else 'application/octet-stream'
                        if ext == '.wem':
                            audio_type = 'audio/wem'
                        self.send_json({
                            'type': 'audio',
                            'name': os.path.basename(target),
                            'size': size,
                            'ext': ext,
                            'data': b64,
                            'mimeType': audio_type,
                        })
                    except Exception as e:
                        self.send_json({'error': str(e)}, 500)

                elif is_video:
                    self.send_json({
                        'type': 'binary',
                        'name': os.path.basename(target),
                        'size': size,
                        'ext': ext,
                        'note': 'Bink video - not playable in browser',
                    })

                elif is_image:
                    try:
                        with open(target, 'rb') as f:
                            raw = f.read()
                        b64 = base64.b64encode(raw).decode()
                        mime = mimetypes.guess_type(target)[0] or 'image/png'
                        self.send_json({
                            'type': 'image',
                            'name': os.path.basename(target),
                            'size': size,
                            'ext': ext,
                            'data': b64,
                            'mimeType': mime,
                        })
                    except Exception as e:
                        self.send_json({'error': str(e)}, 500)

                else:
                    try:
                        with open(target, 'rb') as f:
                            raw = f.read()
                        b64 = base64.b64encode(raw).decode()
                        hex_preview = raw[:256].hex()
                        self.send_json({
                            'type': 'binary',
                            'name': os.path.basename(target),
                            'size': size,
                            'ext': ext,
                            'data': b64,
                            'hexPreview': hex_preview,
                        })
                    except Exception as e:
                        self.send_json({'error': str(e)}, 500)

            elif path == '/api/zip':
                rel = params.get('path', [''])[0]
                target = safe_path(rel)
                if not target or not os.path.isfile(target):
                    self.send_json({'error': 'File not found'}, 404)
                    return
                try:
                    entries = []
                    with zipfile.ZipFile(target, 'r') as z:
                        for info in z.infolist():
                            entries.append({
                                'name': info.filename,
                                'size': info.file_size,
                                'compressed': info.compress_size,
                            })
                    self.send_json({
                        'type': 'zip',
                        'name': os.path.basename(target),
                        'path': rel,
                        'entries': entries,
                    })
                except Exception as e:
                    self.send_json({'error': str(e)}, 500)

            elif path == '/api/zipfile':
                rel = params.get('path', [''])[0]
                inner = params.get('file', [''])[0]
                target = safe_path(rel)
                if not target or not os.path.isfile(target):
                    self.send_json({'error': 'File not found'}, 404)
                    return
                try:
                    with zipfile.ZipFile(target, 'r') as z:
                        raw = z.read(inner)
                    if len(raw) > 50 * 1024 * 1024:
                        self.send_json({'error': 'File too large'}, 400)
                        return
                    ext = os.path.splitext(inner)[1].lower()
                    b64 = base64.b64encode(raw).decode()
                    is_bytecode = len(raw) >= 5 and raw[:4] == b'\x1bLua'
                    is_text_ext = ext in ('.lua', '.sx', '.js', '.xml', '.json', '.txt', '.cfg', '.ini', '.py', '.html', '.css', '.as')
                    if is_text_ext and not is_bytecode:
                        try:
                            content = raw.decode('utf-8')
                            self.send_json({
                                'type': 'text',
                                'containerPath': rel,
                                'name': os.path.basename(inner),
                                'size': len(raw),
                                'ext': ext,
                                'content': content,
                            })
                        except UnicodeDecodeError:
                            self.send_json({
                                'type': 'binary',
                                'name': os.path.basename(inner),
                                'size': len(raw),
                                'ext': ext,
                                'data': b64,
                                'hexPreview': raw[:256].hex(),
                                'mimeType': 'application/octet-stream',
                            })
                    else:
                        self.send_json({
                            'type': 'binary',
                            'name': os.path.basename(inner),
                            'size': len(raw),
                            'ext': ext,
                            'data': b64,
                            'hexPreview': raw[:256].hex(),
                            'mimeType': 'application/octet-stream',
                        })
                except Exception as e:
                    self.send_json({'error': str(e)}, 500)

            elif path == '/api/search':
                query = params.get('q', [''])[0].lower()
                if len(query) < 2:
                    self.send_json({'error': 'Query too short'}, 400)
                    return
                results = []
                max_results = 100
                for root, dirs, files in os.walk(ROMFS_DIR):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    if len(results) >= max_results:
                        break
                    for fname in files:
                        if fname.startswith('.'):
                            continue
                        if query in fname.lower():
                            full = os.path.join(root, fname)
                            rel = os.path.relpath(full, ROMFS_DIR)
                            results.append({
                                'name': fname,
                                'path': rel,
                                'size': os.path.getsize(full),
                                'ext': os.path.splitext(fname)[1].lower(),
                            })
                            if len(results) >= max_results:
                                break
                self.send_json({'query': query, 'results': results})

            elif path == '/api/structure':
                def tree(d, depth=0, max_depth=2):
                    if depth >= max_depth:
                        return []
                    items = []
                    try:
                        for name in sorted(os.listdir(d)):
                            if name.startswith('.'):
                                continue
                            full = os.path.join(d, name)
                            if os.path.isdir(full):
                                children = tree(full, depth+1, max_depth)
                                items.append({'name': name, 'type': 'dir', 'children': children})
                            else:
                                items.append({'name': name, 'type': 'file', 'size': os.path.getsize(full)})
                    except PermissionError:
                        pass
                    return items
                self.send_json({'structure': tree(ROMFS_DIR)})

            elif path == '/api/scripts':
                self.send_json({'scripts': scan_romfs_scripts()})

            elif path == '/api/audio':
                exts = {'.bnk', '.wem', '.pck'}
                self.send_json({'audio': scan_romfs_by_exts(exts)})

            elif path == '/api/media':
                exts = {'.bik', '.mp4', '.webm'}
                self.send_json({'media': scan_romfs_by_exts(exts)})

            elif path == '/api/data':
                exts = {'.bin', '.manifest', '.tszip', '.zip', '.bdp', '_weapons', '_fonts', '_dna', '_cars3'}
                self.send_json({'data': scan_romfs_by_exts(exts)})

            elif path == '/api/mod/workspace':
                self.send_json({'files': mod_list_workspace()})

            elif path == '/api/mod/file':
                rel = params.get('path', [''])[0]
                if not rel:
                    self.send_json({'error': 'Missing path'}, 400)
                    return
                target = mod_workspace_path(rel)
                if not target or not os.path.isfile(target):
                    self.send_json({'error': 'File not found'}, 404)
                    return
                ext = os.path.splitext(target)[1].lower()
                with open(target, 'rb') as f:
                    raw = f.read()
                is_text = ext in ('.lua', '.txt', '.xml', '.json', '.js', '.cfg', '.ini', '.py', '.html', '.css') or ext == ''
                if is_text:
                    self.send_json({
                        'type': 'text', 'name': os.path.basename(target),
                        'size': len(raw), 'ext': ext,
                        'content': raw.decode('utf-8', errors='replace'),
                    })
                else:
                    b64 = base64.b64encode(raw).decode()
                    is_img = ext in ('.png', '.jpg', '.jpeg', '.bmp', '.tga', '.dds')
                    self.send_json({
                        'type': 'image' if is_img else 'binary',
                        'name': os.path.basename(target),
                        'size': len(raw), 'ext': ext,
                        'data': b64,
                    })

            elif path == '/api/mod/romfs-zip':
                rel = params.get('path', [''])[0]
                target = safe_path(rel)
                if not target or not os.path.isfile(target):
                    self.send_json({'error': 'ZIP not found'}, 404)
                    return
                try:
                    with zipfile.ZipFile(target, 'r') as z:
                        entries = []
                        for info in z.infolist():
                            if info.is_dir():
                                continue
                            entries.append({
                                'name': info.filename,
                                'size': info.file_size,
                                'ext': os.path.splitext(info.filename)[1].lower(),
                            })
                    self.send_json({'zipPath': rel, 'name': os.path.basename(target), 'entries': entries})
                except Exception as e:
                    self.send_json({'error': str(e)}, 500)

            elif path == '/api/mod/compile':
                self.send_json({'note': 'Use POST for compilation'}, 400)

            elif path == '/api/mod/export-check':
                items = mod_list_workspace()
                self.send_json({'modFiles': len(items), 'workspace': MOD_WORKSPACE})

            else:
                self.send_error(404)

        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({'error': str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        content_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_len) if content_len > 0 else b''

        try:
            if path == '/api/mod/save':
                data = json.loads(body)
                rel = data.get('path', '')
                content = data.get('content', '')
                if not rel:
                    self.send_json({'error': 'Missing path'}, 400)
                    return
                result, err = mod_save_text(rel, content)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(result)

            elif path == '/api/mod/save-binary':
                data = json.loads(body)
                rel = data.get('path', '')
                b64 = data.get('data', '')
                if not rel:
                    self.send_json({'error': 'Missing path'}, 400)
                    return
                result, err = mod_save_file(rel, b64)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(result)

            elif path == '/api/mod/extract':
                data = json.loads(body)
                zip_path = data.get('zipPath', '')
                inner = data.get('inner', '')
                dest = data.get('dest', '')
                if not zip_path or not inner or not dest:
                    self.send_json({'error': 'Missing zipPath, inner, or dest'}, 400)
                    return
                result, err = mod_extract_from_zip(zip_path, inner, dest)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(result)

            elif path == '/api/mod/compile':
                data = json.loads(body)
                source = data.get('source', '')
                filename = data.get('filename', 'script.lua')
                if not source:
                    self.send_json({'error': 'Missing source'}, 400)
                    return
                bytecode, err = lua_compile(source, filename)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json({
                    'bytecode': base64.b64encode(bytecode).decode(),
                    'size': len(bytecode),
                })

            elif path == '/api/mod/disassemble':
                data = json.loads(body)
                b64data = data.get('bytecode', '')
                if not b64data:
                    self.send_json({'error': 'Missing bytecode'}, 400)
                    return
                raw = base64.b64decode(b64data)
                disasm = disassemble_lua51(raw)
                if disasm:
                    self.send_json({'disassembly': disasm})
                else:
                    self.send_json({'error': 'Failed to disassemble'})



            elif path == '/api/mod/encode-tex':
                data = json.loads(body)
                rgba_b64 = data.get('rgba', '')
                width = data.get('width', 0)
                height = data.get('height', 0)
                fmt = data.get('format', 'BC3')
                if not rgba_b64 or width <= 0 or height <= 0:
                    self.send_json({'error': 'Missing rgba, width, or height'}, 400)
                    return
                rgba_raw = base64.b64decode(rgba_b64)
                if fmt == 'BC3':
                    encoded = encode_bc3(rgba_raw, width, height)
                elif fmt == 'RGBA8':
                    encoded = encode_rgba8(rgba_raw, width, height)
                else:
                    self.send_json({'error': f'Encoding not supported for {fmt}'}, 400)
                    return
                self.send_json({
                    'data': base64.b64encode(encoded).decode(),
                    'size': len(encoded),
                    'format': fmt,
                })

            elif path == '/api/mod/export':
                data = json.loads(body)
                mod_name = data.get('name', 'cars3_mod')
                result, err = mod_export_ryujinx(mod_name)
                if err:
                    self.send_json({'error': err}, 500)
                    return
                self.send_json(result)

            elif path == '/api/mod/repack':
                data = json.loads(body)
                zip_path = data.get('zipPath', '')
                files = data.get('files', [])
                result, err = mod_repack_into_zip(files, zip_path)
                if err:
                    self.send_json({'error': err}, 500)
                    return
                self.send_json(result)

            elif path == '/api/mod/delete':
                data = json.loads(body)
                rel = data.get('path', '')
                target = mod_workspace_path(rel)
                if target and os.path.isfile(target):
                    os.unlink(target)
                    self.send_json({'deleted': rel})
                else:
                    self.send_json({'error': 'File not found'}, 404)

            else:
                self.send_error(404)

        except json.JSONDecodeError:
            self.send_json({'error': 'Invalid JSON'}, 400)
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({'error': str(e)}, 500)
            except Exception:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, filepath, content_type):
        try:
            with open(filepath, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', len(body))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == '__main__':
    print(f'Cars 3 Viewer on http://127.0.0.1:{PORT}')
    ThreadedHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
