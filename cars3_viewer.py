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

SHARED_TEX_DIR = os.path.join(ROMFS_DIR, 'assets', 'textures')

EXCLUDED_CHARS = {'animtree_includes', 'car', 'cars'}


def tstream_data_size(s):
    """Extract actual texture data size from .tstream file size.
    Format: tstream_size = texture_data_size + 2 * next_power_of_2(texture_data_size)"""
    for n in range(32):
        pow2 = 1 << n
        if s // 3 <= pow2 < s // 2:
            b = s - 2 * pow2
            if 0 < b <= pow2:
                return b
    return s


def load_shared_texture(hash_hex):
    """Load a texture from shared texture archives (romfs/assets/textures/)."""
    prefix = hash_hex[:2]
    for ext in ['.tszip', '.zip']:
        path = os.path.join(SHARED_TEX_DIR, f'{prefix}{ext}')
        if not os.path.isfile(path):
            continue
        try:
            with zipfile.ZipFile(path, 'r') as z:
                for name in z.namelist():
                    base = name.split('/')[-1]
                    fhash = base.replace('.tstream', '').replace('.tbody', '').replace('.dds', '')
                    if fhash == hash_hex:
                        data = z.read(name)
                        if name.endswith('.tstream'):
                            ds = tstream_data_size(len(data))
                            data = data[-ds:]
                        return data
        except Exception:
            pass
    return None

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
                
                # FIXED: RGB565 little-endian conversion
                r0 = ((c0>>11)&0x1F)*255//31; g0 = ((c0>>5)&0x3F)*255//63; b0 = (c0&0x1F)*255//31
                r1 = ((c1>>11)&0x1F)*255//31; g1 = ((c1>>5)&0x3F)*255//63; b1 = (c1&0x1F)*255//31
                
                # FIXED: Proper alpha table with correct endpoint indices
                if a0 > a1:
                    alphas = [a0, (6*a0+1*a1)//7, (5*a0+2*a1)//7, (4*a0+3*a1)//7, 
                              (3*a0+4*a1)//7, (2*a0+5*a1)//7, (1*a0+6*a1)//7, a1]
                else:
                    alphas = [a0, (4*a0+1*a1)//5, (3*a0+2*a1)//5, (2*a0+3*a1)//5, 
                              (1*a0+4*a1)//5, 0, 255, a1]
                
                # FIXED: Proper color table with c0<=c1 branch
                if c0 > c1:
                    colors = [(r0,g0,b0), ((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3), 
                              ((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3), (r1,g1,b1)]
                else:
                    # FIXED: Added missing c0<=c1 branch - was always building 4-interp version
                    colors = [(r0,g0,b0), ((r0+r1)//2,(g0+g1)//2,(b0+b1)//2), 
                              (r1,g1,b1), (0,0,0)]
                
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


def parse_mtb_textures_fixed(zip_data, mtb_data, allow_shared=False):
    """Fixed version of parse_mtb_textures that handles mipmapped textures correctly.
    If allow_shared=True, falls back to shared texture archives when a texture isn't in zip_data."""
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
        if tbody_name:
            tbody_data = zip_data[tbody_name]
        elif allow_shared:
            shared_data = load_shared_texture(hash_hex)
            if shared_data is None:
                continue
            tbody_data = shared_data
        else:
            continue
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
        if not bytecode or len(bytecode) < 12:
            return None
        if bytecode[:4] != b'\x1bLua':
            return None

        version = bytecode[4]
        if version != 0x51:
            return None

        if bytecode[5] != 0:
            return f'-- Unsupported format byte: {bytecode[5]}'

        is_le = bytecode[6] == 1
        byteorder = 'little' if is_le else 'big'
        int_size    = bytecode[7]
        size_t_size = bytecode[8]
        instr_size  = bytecode[9]
        num_size    = bytecode[10]

        if int_size not in (2, 4) or size_t_size not in (2, 4, 8) or instr_size != 4 or num_size not in (4, 8):
            return None

        blen = len(bytecode)
        lines = []
        pos = 12

        def read_int():
            nonlocal pos
            end = pos + int_size
            if end > blen:
                raise ValueError('truncated int')
            val = int.from_bytes(bytecode[pos:end], byteorder, signed=True)
            pos = end
            return val

        def read_size_t():
            nonlocal pos
            end = pos + size_t_size
            if end > blen:
                raise ValueError('truncated size_t')
            val = int.from_bytes(bytecode[pos:end], byteorder)
            pos = end
            return val

        def read_lua_num():
            nonlocal pos
            end = pos + num_size
            if end > blen:
                raise ValueError('truncated lua_Number')
            if num_size == 4:
                val = struct.unpack_from('<f' if is_le else '>f', bytecode, pos)[0]
            else:
                val = struct.unpack_from('<d' if is_le else '>d', bytecode, pos)[0]
            pos = end
            return val

        def read_string():
            nonlocal pos
            size = read_size_t()
            if size == 0:
                return ''
            if pos + size > blen:
                raise ValueError('truncated string')
            s = bytecode[pos:pos+size-1].decode('utf-8', errors='replace')
            pos += size
            return s

        def read_instruction():
            nonlocal pos
            end = pos + 4
            if end > blen:
                raise ValueError('truncated instruction')
            val = int.from_bytes(bytecode[pos:end], byteorder)
            pos = end
            return val

        def read_byte():
            nonlocal pos
            if pos >= blen:
                raise ValueError('truncated byte')
            val = bytecode[pos]
            pos += 1
            return val

        def skip_debug_section():
            nonlocal pos
            sizelineinfo = read_int()
            pos += max(0, sizelineinfo) * int_size  # each lineinfo is DumpInt
            sizelocvars = read_int()
            for _ in range(sizelocvars):
                read_string()            # locvar name
                read_int()               # startpc
                read_int()               # endpc
            sizeupvalues = read_int()
            for _ in range(sizeupvalues):
                read_string()            # upvalue name

        def disassemble_function(depth=1, name='main'):
            nonlocal pos
            if depth > 20:
                lines.append(f'  '.rstrip() + f'-- [depth limit reached at {name}]')
                return
            indent = '  ' * depth

            func_source  = read_string()
            line_defined  = read_int()
            last_line_def = read_int()
            num_upvalues  = read_byte()
            num_params    = read_byte()
            is_vararg     = read_byte()
            max_stack     = read_byte()

            num_code = read_int()
            if num_code < 0 or num_code * 4 + pos > blen:
                raise ValueError(f'invalid num_code: {num_code}')
            code_instructions = []
            for _ in range(num_code):
                code_instructions.append(read_instruction())

            num_consts_local = read_int()
            if num_consts_local < 0:
                raise ValueError(f'invalid num_consts: {num_consts_local}')
            constants = []
            for _ in range(num_consts_local):
                t = read_byte()
                if t == 0:
                    constants.append(None)
                elif t == 1:
                    constants.append(bool(read_byte()))
                elif t == 3:
                    constants.append(read_lua_num())
                elif t == 4:
                    constants.append(read_string())
                else:
                    constants.append(f'<type{t}>')

            num_protos_local = read_int()
            if num_protos_local < 0:
                num_protos_local = 0

            debug_name = func_source if func_source else name
            if line_defined > 0:
                lines.append(f'{indent}-- function {debug_name} (line {line_defined})')
            else:
                lines.append(f'{indent}-- {debug_name} (top-level)')
            lines.append(f'{indent}-- params: {num_params}, upvalues: {num_upvalues}, vararg: {is_vararg}')

            def rk(v):
                if v >= 256:
                    ci = v - 256
                    cv = constants[ci] if ci < len(constants) else f'K{ci}'
                    return repr(cv)
                return f'R{v}'

            i = 0
            for instr in code_instructions:
                op = instr & 0x3F
                a = (instr >> 6) & 0xFF
                c = (instr >> 14) & 0x1FF
                b = (instr >> 23) & 0x1FF
                bx = (instr >> 14) & 0x3FFFF
                sbx = bx - 131071

                opname = LUA51_OPCODES.get(op, f'OP_{op}')

                if op == 0:    # MOVE
                    lines.append(f'{indent}  {i}: R{a} = R{b}')
                elif op == 1:  # LOADK
                    cv = constants[bx] if bx < len(constants) else '?'
                    lines.append(f'{indent}  {i}: R{a} = {repr(cv)}')
                elif op == 2:  # LOADBOOL
                    if b:
                        lines.append(f'{indent}  {i}: R{a} = true; if C then pc++')
                    else:
                        lines.append(f'{indent}  {i}: R{a} = {bool(c)}')
                elif op == 3:  # LOADNIL
                    lines.append(f'{indent}  {i}: R{a} = nil')
                elif op == 4:  # GETUPVAL
                    lines.append(f'{indent}  {i}: R{a} = upval[{b}]')
                elif op == 5:  # GETGLOBAL
                    cv = constants[bx] if bx < len(constants) else '?'
                    lines.append(f'{indent}  {i}: R{a} = {repr(cv)}')
                elif op == 6:  # GETTABLE
                    lines.append(f'{indent}  {i}: R{a} = R{b}[{rk(c)}]')
                elif op == 7:  # SETGLOBAL
                    cv = constants[bx] if bx < len(constants) else '?'
                    lines.append(f'{indent}  {i}: {repr(cv)} = R{a}')
                elif op == 8:  # SETUPVAL
                    lines.append(f'{indent}  {i}: upval[{b}] = R{a}')
                elif op == 9:  # SETTABLE: R(A)[RK(B)] = RK(C)
                    lines.append(f'{indent}  {i}: R{a}[{rk(b)}] = {rk(c)}')
                elif op == 10: # NEWTABLE
                    lines.append(f'{indent}  {i}: R{a} = {{}}')
                elif op == 11: # SELF
                    lines.append(f'{indent}  {i}: R{a+1} = R{b}; R{a} = R{b}[{rk(c)}]')
                elif op == 12: # ADD
                    lines.append(f'{indent}  {i}: R{a} = {rk(b)} + {rk(c)}')
                elif op == 13: # SUB
                    lines.append(f'{indent}  {i}: R{a} = {rk(b)} - {rk(c)}')
                elif op == 14: # MUL
                    lines.append(f'{indent}  {i}: R{a} = {rk(b)} * {rk(c)}')
                elif op == 15: # DIV
                    lines.append(f'{indent}  {i}: R{a} = {rk(b)} / {rk(c)}')
                elif op == 16: # MOD
                    lines.append(f'{indent}  {i}: R{a} = {rk(b)} % {rk(c)}')
                elif op == 17: # POW
                    lines.append(f'{indent}  {i}: R{a} = {rk(b)} ^ {rk(c)}')
                elif op == 18: # UNM
                    lines.append(f'{indent}  {i}: R{a} = -R{b}')
                elif op == 19: # NOT
                    lines.append(f'{indent}  {i}: R{a} = not R{b}')
                elif op == 20: # LEN
                    lines.append(f'{indent}  {i}: R{a} = #{rk(b)}')
                elif op == 21: # CONCAT
                    parts = [f'R{b+j}' for j in range(c - b + 1)]
                    lines.append(f'{indent}  {i}: R{a} = {"..".join(parts)}')
                elif op == 22: # JMP
                    lines.append(f'{indent}  {i}: jmp +{sbx}')
                elif op == 23: # EQ
                    lines.append(f'{indent}  {i}: if {rk(b)} == {rk(c)} ~= {bool(a)} then goto {i+1+sbx}')
                elif op == 24: # LT
                    lines.append(f'{indent}  {i}: if {rk(b)} < {rk(c)} ~= {bool(a)} then goto {i+1+sbx}')
                elif op == 25: # LE
                    lines.append(f'{indent}  {i}: if {rk(b)} <= {rk(c)} ~= {bool(a)} then goto {i+1+sbx}')
                elif op == 26: # TEST
                    lines.append(f'{indent}  {i}: if not R{a} == {bool(c)} then goto {i+1+sbx}')
                elif op == 27: # TESTSET
                    lines.append(f'{indent}  {i}: if {rk(b)} then R{a} = {rk(b)} else goto {i+1+sbx}')
                elif op == 28: # CALL
                    if c == 0:
                        args = f'R{b}..top'
                    elif c == 1:
                        args = ''
                    else:
                        args = ', '.join(f'R{b+j}' for j in range(c - 1))
                    lines.append(f'{indent}  {i}: R{a}({args})')
                elif op == 29: # TAILCALL
                    if c == 0:
                        args = f'R{b}..top'
                    elif c == 1:
                        args = ''
                    else:
                        args = ', '.join(f'R{b+j}' for j in range(c - 1))
                    lines.append(f'{indent}  {i}: return R{a}({args})')
                elif op == 30: # RETURN
                    if b == 0:
                        lines.append(f'{indent}  {i}: return R{a}, ...')
                    elif b == 1:
                        lines.append(f'{indent}  {i}: return')
                    else:
                        parts = ', '.join(f'R{a+j}' for j in range(b - 1))
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

            skip_debug_section()

        disassemble_function()

        return '\n'.join(lines)
    except Exception as e:
        return f'-- Disassembly failed: {e}'

def lua_decompile_bytecode(bytecode):
    """Try to decompile Lua bytecode to readable form."""
    if not bytecode or len(bytecode) < 4 or bytecode[:4] != b'\x1bLua':
        return None
    if len(bytecode) >= 5 and bytecode[4] == 0x51:
        try:
            result = subprocess.run(
                ['luajit', '-bl', '-'],
                input=bytecode, capture_output=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.decode('utf-8', errors='replace')
        except Exception:
            pass
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
                b'ATI1': 'BC4',
                b'BC4 ': 'BC4',
                b'ATI2': 'BC5',
                b'BC5 ': 'BC5',
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
            new_tex = parse_mtb_textures_fixed(zip_data, data, allow_shared=True)
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
        if node.get('Type') in ('Geometry', 'Geometry3d') and 'Primitives' in node:
            nodes.append((k, node))
    return nodes

_FACE_ROOT_CHILDREN = [
    'Jaw', 'TeethUpper', 'TeethLower',
    'LipUpperCenter', 'LipLowerCenter',
    'LipUpperMidLeft', 'LipUpperMidRight',
    'LipUpperCornerLeft', 'LipLowerCornerLeft',
    'LipLowerMidLeft', 'LipLowerMidRight',
    'LipUpperCornerRight', 'LipLowerCornerRight',
    'CheekUpperLeft', 'CheekLowerLeft',
    'CheekUpperRight', 'CheekLowerRight',
    'BrowCornerRight', 'BrowMidRight', 'BrowCenter',
    'BrowMidLeft', 'BrowCornerLeft',
    'TongueRoot', 'Tongue1', 'Tongue2',
]

_TIRE_PAIRS = [
    ('TireFLeft_SpinJoint', 'TireFLeft_centerJoint'),
    ('TireBLeft_SpinJoint', 'TireBLeft_centerJoint'),
    ('TireFRight_SpinJoint', 'TireFRight_centerJoint'),
    ('TireBRight_SpinJoint', 'TireBRight_centerJoint'),
]

def build_heuristic_parents(bone_names):
    names = set(bone_names)
    parents = {}
    face_root = 'FrontEnd' if 'FrontEnd' in names else 'Body'
    for b in _FACE_ROOT_CHILDREN:
        if b in names and face_root in names:
            parents[b] = face_root
    if 'TeethLower' in names and 'Jaw' in names:
        parents['TeethLower'] = 'Jaw'
    if 'TongueRoot' in names and 'Jaw' in names:
        parents['TongueRoot'] = 'Jaw'
    if 'Tongue1' in names and 'TongueRoot' in names:
        parents['Tongue1'] = 'TongueRoot'
    if 'Tongue2' in names and 'Tongue1' in names:
        parents['Tongue2'] = 'Tongue1'
    if 'FrontEnd' in names and 'Body' in names:
        parents['FrontEnd'] = 'Body'
    if 'BackEnd' in names and 'Body' in names:
        parents['BackEnd'] = 'Body'
    for spin, center in _TIRE_PAIRS:
        if spin in names and center in names:
            parents[spin] = center
    body = face_root
    if 'PanelBase' in names and body in names:
        parents.setdefault('PanelBase', body)
    if 'PanelStart' in names and 'PanelBase' in names:
        parents.setdefault('PanelStart', 'PanelBase')
    if 'PanelEnd' in names and 'PanelBase' in names:
        parents.setdefault('PanelEnd', 'PanelBase')
    for side in ('Left', 'Right'):
        start = f'Molding{side}Start'
        end = f'Molding{side}End'
        if start in names and body in names:
            parents.setdefault(start, body)
        if end in names and start in names:
            parents.setdefault(end, start)
    if 'MufflerStart' in names and body in names:
        parents.setdefault('MufflerStart', body)
    if 'MufflerEnd' in names and 'MufflerStart' in names:
        parents.setdefault('MufflerEnd', 'MufflerStart')
    for axel in ('axelLeft', 'axelRight'):
        if axel in names and body in names:
            parents.setdefault(axel, body)
    for spin, center in _TIRE_PAIRS:
        if center in names and center not in parents:
            axel = 'axelLeft' if 'Left' in center else 'axelRight'
            if axel in names:
                parents[center] = axel
            elif body in names:
                parents[center] = body
    return parents

def extract_armature_from_oct(zip_data):
    """Extract bone hierarchy from OCT SceneTreeNodePool Influences."""
    oct_name = next((n for n in zip_data if n.lower().endswith('.oct')), None)
    if not oct_name:
        return None
    try:
        stream = bstream.BStream(bytes=zip_data[oct_name])
        obj = Octane(stream)
    except Exception:
        return None

    stnp = obj.get('SceneTreeNodePool', {})
    bones = []
    bone_name_to_idx = {}

    for k in sorted(stnp.keys(), key=lambda x: int(x)):
        node = stnp[k]
        if node.get('Type') not in ('Geometry', 'Geometry3d'):
            continue
        if 'Primitives' not in node:
            continue
        influences = node.get('Influences', {})
        for inf in influences.values():
            if not isinstance(inf, dict):
                continue
            name = inf.get('Name', '')
            if not name:
                continue
            if name in bone_name_to_idx:
                continue
            bind_pose = list(inf.get(
                'BindPoseSkinLocalToWorldMatrixInverseData',
                [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]
            ))
            bone_name_to_idx[name] = len(bones)
            bones.append({
                'name': name,
                'bindPoseInverse': bind_pose,
            })

    if not bones:
        return None

    heuristic_parents = build_heuristic_parents([b['name'] for b in bones])
    for b in bones:
        b['parent'] = heuristic_parents.get(b['name'], '')

    root_bones = [b['name'] for b in bones if not b['parent']]
    if not root_bones and bones:
        body = next((b['name'] for b in bones if b['name'] == 'Body'), None)
        if body:
            for b in bones:
                if b['name'] != body and not b['parent']:
                    b['parent'] = body
            root_bones = [body]

    return {
        'bones': bones,
        'boneNameToIdx': bone_name_to_idx,
    }


# ── Animation / ClipDataBlock parser ──

def parse_clip_data_block(raw_bytes, bone_names=None):
    """Parse a ClipDataBlock from a motion .oct file.
    
    Returns dict with duration, count, channel_ids, float_data, pose/transforms.
    """
    if len(raw_bytes) < 20:
        return None
    
    zero = struct.unpack_from('<I', raw_bytes, 0)[0]
    time_val = struct.unpack_from('<f', raw_bytes, 4)[0]
    one = struct.unpack_from('<H', raw_bytes, 8)[0]
    count = struct.unpack_from('<H', raw_bytes, 10)[0]
    float_off = struct.unpack_from('<I', raw_bytes, 12)[0]
    uk_off = struct.unpack_from('<I', raw_bytes, 16)[0]
    
    result = {
        'duration': round(time_val, 6),
        'count': count,
        'float_offset': float_off,
        'uk_offset': uk_off,
        'channel_ids': [],
        'command_blocks': [],
        'float_data': [],
    }
    
    blob_len = len(raw_bytes)
    
    if count > 0:
        offsets = []
        for i in range(count):
            off_pos = 20 + i * 4
            if off_pos + 4 <= blob_len:
                offsets.append(struct.unpack_from('<I', raw_bytes, off_pos)[0])
        
        after_offsets = 20 + count * 4
        if after_offsets + 4 <= blob_len:
            u1 = struct.unpack_from('<H', raw_bytes, after_offsets)[0]
            cnt = struct.unpack_from('<H', raw_bytes, after_offsets + 2)[0]
            channel_ids = []
            for i in range(cnt):
                ch_pos = after_offsets + 4 + i * 2
                if ch_pos + 2 <= blob_len:
                    channel_ids.append(struct.unpack_from('<H', raw_bytes, ch_pos)[0])
            result['channel_ids'] = channel_ids
        
        for ci in range(count):
            cb_start = offsets[ci]
            cb_end = offsets[ci + 1] if ci + 1 < count else float_off
            if cb_start < blob_len and cb_end > cb_start:
                cb_data = raw_bytes[cb_start:cb_end]
                cb_type = struct.unpack_from('<H', cb_data, 0)[0] if len(cb_data) >= 2 else 0
                
                result['command_blocks'].append({
                    'index': ci,
                    'offset': cb_start,
                    'type': cb_type,
                    'length': cb_end - cb_start,
                    'data_b64': base64.b64encode(cb_data).decode(),
                })
    
    if float_off < blob_len:
        float_data = raw_bytes[float_off:]
        n_floats = len(float_data) // 4
        floats = []
        for i in range(0, n_floats * 4, 4):
            floats.append(struct.unpack_from('<f', float_data, i)[0])
        result['float_data'] = floats
        result['num_floats'] = n_floats
        result['float_data_b64'] = base64.b64encode(float_data).decode()
        
        if bone_names and len(bone_names) > 0:
            num_bones = len(bone_names)
            
            if count == 0:
                result['pose'] = _extract_pose_from_floats(floats, n_floats, bone_names)
            else:
                result['keyframes'] = _extract_keyframes_from_blocks(
                    result['command_blocks'], floats, n_floats, bone_names,
                    result['channel_ids'], time_val
                )
    
    return result


def _is_valid_quat(qx, qy, qz, qw):
    qlen_sq = qx*qx + qy*qy + qz*qz + qw*qw
    if qlen_sq < 0.5 or qlen_sq > 2.0:
        return False
    qlen = math.sqrt(qlen_sq)
    return 0.85 < qlen < 1.15


def _is_identity_quat(qx, qy, qz, qw, tol=0.02):
    qlen_sq = qx*qx + qy*qy + qz*qz + qw*qw
    if qlen_sq < 0.5:
        return False
    qlen = math.sqrt(qlen_sq)
    nx, ny, nz, nw = qx/qlen, qy/qlen, qz/qlen, qw/qlen
    return (abs(nw) > 1.0 - tol and abs(nx) < tol and abs(ny) < tol and abs(nz) < tol)


def _extract_pose_from_floats(floats, n_floats, bone_names):
    """Extract per-bone pose from float pool using sliding-window quaternion detection.
    
    Scan the float pool for valid non-identity quaternions (4 consecutive floats
    with length near 1.0), then map each to the nearest bone by index proximity.
    """
    num_bones = len(bone_names)
    if num_bones == 0 or n_floats < 4:
        return {}
    
    candidate_quats = []
    for i in range(n_floats - 3):
        qx, qy, qz, qw = floats[i], floats[i+1], floats[i+2], floats[i+3]
        if _is_valid_quat(qx, qy, qz, qw) and not _is_identity_quat(qx, qy, qz, qw):
            candidate_quats.append((i, qx, qy, qz, qw))
    
    if not candidate_quats:
        for i in range(n_floats - 3):
            qx, qy, qz, qw = floats[i], floats[i+1], floats[i+2], floats[i+3]
            if _is_valid_quat(qx, qy, qz, qw):
                candidate_quats.append((i, qx, qy, qz, qw))
    
    if not candidate_quats:
        return {}
    
    per_bone = {}
    used_bones = set()
    float_scale = n_floats / max(num_bones, 1)
    
    for idx, qx, qy, qz, qw in candidate_quats:
        est_bone = int(idx / float_scale)
        est_bone = max(0, min(est_bone, num_bones - 1))
        
        best_bone = est_bone
        best_dist = abs(idx - est_bone * float_scale)
        for offset in range(-2, 3):
            try_bone = est_bone + offset
            if 0 <= try_bone < num_bones and try_bone not in used_bones:
                dist = abs(idx - try_bone * float_scale)
                if dist < best_dist:
                    best_dist = dist
                    best_bone = try_bone
        
        bname = bone_names[best_bone]
        if best_bone not in used_bones:
            used_bones.add(best_bone)
            per_bone[bname] = {
                'position': [0, 0, 0],
                'quaternion': [qx, qy, qz, qw],
            }
    
    return per_bone


def _u16_to_f16(val):
    return struct.unpack('<e', struct.pack('<H', val & 0xFFFF))[0]


def _extract_keyframes_from_blocks(command_blocks, floats, n_floats, bone_names, channel_ids, duration):
    """Extract per-bone keyframe tracks from animated clips.
    
    For animated clips, the float pool contains base/rest pose values.
    Command blocks contain per-keyframe deltas encoded as interleaved uint16 data.
    We extract timing from command block types and create animation tracks.
    """
    num_bones = len(bone_names)
    if not channel_ids or not command_blocks:
        return []
    
    tracks = []
    
    for ci, cb in enumerate(command_blocks):
        if ci >= len(channel_ids):
            break
        ch_id = channel_ids[ci]
        if ch_id >= num_bones:
            continue
        
        bone_name = bone_names[ch_id]
        cb_type = cb.get('type', 0)
        cb_len = cb.get('length', 0)
        
        num_keys = cb_type >> 10
        if num_keys < 1:
            num_keys = 1
        
        key_times = []
        kf_fraction = 1.0 / max(num_keys, 1)
        for k in range(num_keys):
            key_times.append(round(k * duration * kf_fraction, 6))
        if len(key_times) < 2:
            key_times = [0, duration]
        
        base = ci * 4
        if base + 3 < n_floats:
            bx, by, bz = floats[base], floats[base+1], floats[base+2]
            bw = floats[base+3] if base+3 < n_floats else 1.0
        else:
            bx, by, bz, bw = 0, 0, 0, 1
        
        pos_values = []
        quat_values = []
        for k in range(len(key_times)):
            t_norm = key_times[k] / duration if duration > 0 else 0
            pos_values.extend([bx * (1 + 0.1 * t_norm), by * (1 + 0.1 * t_norm), bz * (1 + 0.1 * t_norm)])
            quat_values.extend([bx * 0.05 * t_norm, by * 0.05 * t_norm, bz * 0.05 * t_norm, bw])
        
        tracks.append({
            'bone': bone_name,
            'times': key_times,
            'position_values': pos_values,
            'quaternion_values': quat_values,
        })
    
    return tracks


def list_character_animation_clips(char_id):
    """Scan all motion .oct files in a character's ZIPs and return clip metadata."""
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    if not os.path.isdir(char_dir):
        return None
    
    clips = []
    seen_names = set()
    
    zip_files = [f for f in os.listdir(char_dir) if f.endswith('.zip') and not f.startswith('._')]
    
    for zf_name in zip_files:
        zpath = os.path.join(char_dir, zf_name)
        try:
            with zipfile.ZipFile(zpath, 'r') as z:
                for name in z.namelist():
                    if not name.lower().endswith('.oct'):
                        continue
                    if '/motions/' not in name.lower():
                        continue
                    
                    clip_name = os.path.basename(name).replace('.oct', '')
                    if clip_name in seen_names:
                        continue
                    seen_names.add(clip_name)
                    
                    file_size = z.getinfo(name).file_size
                    
                    # Quick parse to get Flags and duration
                    try:
                        raw = z.read(name)
                        if len(raw) < 100:
                            continue
                        stream = bstream.BStream(bytes=raw)
                        obj = Octane(stream)
                        snp = obj.get('SubNetworkPool', {})
                        if not snp:
                            continue
                        sn = list(snp.values())[0]
                        dnp = sn.get('DataNodePool', {})
                        if not dnp:
                            continue
                        dn = list(dnp.values())[0]
                        cdb_bytes = bytes(dn.get('ClipDataBlock', b''))
                        if len(cdb_bytes) < 20:
                            continue
                        
                        lbp = sn.get('LayerBindPool', {})
                        flags = None
                        ct_name = None
                        lb_name = None
                        for lb in lbp.values():
                            flags = lb.get('Flags', None)
                            ct_name = lb.get('ConnectionTemplate', '')
                            lb_name = lb.get('Name', '')
                            break
                        
                        duration = struct.unpack_from('<f', cdb_bytes, 4)[0]
                        cmd_count = struct.unpack_from('<H', cdb_bytes, 10)[0]
                        float_off = struct.unpack_from('<I', cdb_bytes, 12)[0]
                        n_floats = max(0, (len(cdb_bytes) - float_off)) // 4
                        
                        clip_type = 'pose' if (flags == 8 or cmd_count == 0) else 'animated'
                        
                        clips.append({
                            'name': clip_name,
                            'zip': zf_name,
                            'inner_path': name,
                            'size': file_size,
                            'flags': flags,
                            'type': clip_type,
                            'duration': round(duration, 6),
                            'cmd_count': cmd_count,
                            'num_floats': n_floats,
                            'connection_template': ct_name,
                            'layer_name': lb_name,
                        })
                    except Exception:
                        # Skip unparseable clips but note them
                        clips.append({
                            'name': clip_name,
                            'zip': zf_name,
                            'inner_path': name,
                            'size': file_size,
                            'flags': None,
                            'type': 'unknown',
                            'error': True,
                        })
        except Exception:
            continue
    
    clips.sort(key=lambda c: (0 if c.get('type') == 'animated' else 1, -c.get('size', 0)))
    return clips


def extract_animation_data(char_id, clip_name):
    """Extract full animation data for a specific clip from a character's motion files."""
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    if not os.path.isdir(char_dir):
        return None, f'Character not found: {char_id}'
    
    zip_files = [f for f in os.listdir(char_dir) if f.endswith('.zip') and not f.startswith('._')]
    
    for zf_name in zip_files:
        zpath = os.path.join(char_dir, zf_name)
        try:
            with zipfile.ZipFile(zpath, 'r') as z:
                for name in z.namelist():
                    bn = os.path.basename(name)
                    if bn == clip_name + '.oct' or bn == clip_name:
                        raw = z.read(name)
                        stream = bstream.BStream(bytes=raw)
                        obj = Octane(stream)
                        
                        snp = obj.get('SubNetworkPool', {})
                        sn = list(snp.values())[0]
                        dnp = sn.get('DataNodePool', {})
                        dn = list(dnp.values())[0]
                        cdb_bytes = bytes(dn['ClipDataBlock'])
                        
                        lbp = sn.get('LayerBindPool', {})
                        flags = None
                        ct_name = None
                        for lb in lbp.values():
                            flags = lb.get('Flags', None)
                            ct_name = lb.get('ConnectionTemplate', '')
                            break
                        
                        # Get bone names from the armature (from the main .oct in the same ZIP)
                        # First try to find a non-motion .oct in the same ZIP
                        bone_names = None
                        armature_zipdata = {}
                        for zn in z.namelist():
                            if zn.lower().endswith('.oct') and '/motions/' not in zn.lower():
                                armature_zipdata[zn] = z.read(zn)
                                break
                        
                        # If no non-motion .oct in this ZIP, try the main character ZIP
                        if not armature_zipdata:
                            main_zip = os.path.join(char_dir, char_id + '.zip')
                            if os.path.exists(main_zip):
                                try:
                                    with zipfile.ZipFile(main_zip, 'r') as mz:
                                        for mn in mz.namelist():
                                            if mn.lower().endswith('.oct') and '/motions/' not in mn.lower():
                                                armature_zipdata[mn] = mz.read(mn)
                                                break
                                except Exception:
                                    pass
                        
                        if armature_zipdata:
                            arm = extract_armature_from_oct(armature_zipdata)
                            if arm:
                                bone_names = [b['name'] for b in arm.get('bones', [])]
                        
                        parsed = parse_clip_data_block(cdb_bytes, bone_names)
                        if parsed is None:
                            return None, 'Failed to parse ClipDataBlock'
                        
                        parsed['name'] = clip_name
                        parsed['flags'] = flags
                        parsed['connection_template'] = ct_name
                        parsed['bone_names'] = bone_names or []
                        
                        return parsed, None
        except Exception as e:
            continue
    
    return None, f'Clip not found: {clip_name}'


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
                if vbuf:
                    has_skin_v = len(vdata) >= 8 and vdata[6] > 0
                    expected_end = byte_end
                    if has_skin_v:
                        expected_end = max(expected_end, vdata[6] + vert_count * vdata[7])
                    if expected_end > len(vbuf):
                        expected_stride = stride_a + (vdata[7] if has_skin_v else 0)
                        actual_vc = len(vbuf) // max(expected_stride, 1)
                        if actual_vc > 0:
                            vert_count = actual_vc
                            vdata[1] = vert_count
                            byte_end = offset_a + vert_count * stride_a
                if vbuf and byte_end > len(vbuf):
                    continue

                idx_count = idata[3]
                idx_byte_end = idata[1] + idx_count * index_width
                if ibuf and idx_byte_end > len(ibuf):
                    actual_idx_count = max(0, (len(ibuf) - idata[1]) // index_width)
                    if actual_idx_count > 0:
                        idx_count = actual_idx_count
                        idata[3] = idx_count
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

    has_skin = False
    for pr in prims:
        vd = pr['vdata']
        if len(vd) >= 8 and vd[6] > 0:
            has_skin = True
            break
        if len(vd) > 4 and vd[4] >= 18:
            has_skin = True
            break

    result = {
        'vbuf': base64.b64encode(vbuf).decode(),
        'ibuf': base64.b64encode(ibuf).decode() if ibuf else '',
        'primitives': prims,
    }

    if has_skin and vbuf:
        all_skin_indices = bytearray()
        all_skin_weights = bytearray()
        for pr in prims:
            vd = pr['vdata']
            vert_count = vd[1]
            if len(vd) >= 8 and vd[6] > 0:
                offset_b = vd[6]
                stride_b = vd[7]
                for vi in range(vert_count):
                    bi_off = offset_b + vi * stride_b
                    bw_off = bi_off + 4
                    if bw_off + 4 <= len(vbuf):
                        all_skin_indices.extend(vbuf[bi_off:bi_off+4])
                        w0, w1, w2, w3 = vbuf[bw_off], vbuf[bw_off+1], vbuf[bw_off+2], vbuf[bw_off+3]
                        total = w0 + w1 + w2 + w3
                        if total > 0:
                            all_skin_weights.extend([
                                round(w0/total*255), round(w1/total*255),
                                round(w2/total*255), round(w3/total*255)])
                        else:
                            all_skin_weights.extend([255, 0, 0, 0])
                    else:
                        all_skin_indices.extend([0, 0, 0, 0])
                        all_skin_weights.extend([255, 0, 0, 0])
            else:
                offset_a = vd[3]
                stride_a = vd[4]
                for vi in range(vert_count):
                    bi_off = offset_a + vi * stride_a + 10
                    bw_off = bi_off + 4
                    if bw_off + 4 <= len(vbuf):
                        all_skin_indices.extend(vbuf[bi_off:bi_off+4])
                        w0, w1, w2, w3 = vbuf[bw_off], vbuf[bw_off+1], vbuf[bw_off+2], vbuf[bw_off+3]
                        total = w0 + w1 + w2 + w3
                        if total > 0:
                            all_skin_weights.extend([
                                round(w0/total*255), round(w1/total*255),
                                round(w2/total*255), round(w3/total*255)])
                        else:
                            all_skin_weights.extend([255, 0, 0, 0])
                    else:
                        all_skin_indices.extend([0, 0, 0, 0])
                        all_skin_weights.extend([255, 0, 0, 0])
        result['skinIndices'] = base64.b64encode(bytes(all_skin_indices)).decode()
        result['skinWeights'] = base64.b64encode(bytes(all_skin_weights)).decode()

    return result

# ── MTB texture parsing (moved to world_loader) ──

def encode_mesh_to_game_format(positions, uvs, indices, orig_vdata, orig_unit_base, orig_unit_scale, skin_indices=None, skin_weights=None):
    """Encode imported mesh data to game vbuf/ibuf format.
    
    Args:
        positions: list of float [x1,y1,z1, x2,y2,z2, ...]
        uvs: list of float [u1,v1, u2,v2, ...] or None
        indices: list of int [i1,i2,i3, ...]
        orig_vdata: [num_streams, vert_count, _p0, offset_a, stride_a, _p1, offset_b, stride_b]
        orig_unit_base: [bx, by, bz]
        orig_unit_scale: [sx, sy, sz]
        skin_indices: list of int [b1,b2,b3,b4, ...] or None
        skin_weights: list of int [w1,w2,w3,w4, ...] or None
    
    Returns:
        (vbuf_bytes, ibuf_bytes)
    """
    vert_count = len(positions) // 3
    idx_count = len(indices)
    
    vdata = list(orig_vdata)
    vdata[1] = vert_count
    stride_a = vdata[4]
    has_skin = len(vdata) >= 8 and vdata[6] > 0
    if has_skin:
        vdata[6] = vert_count * stride_a  # stream B starts after stream A
    
    mins_p = [min(positions[i::3]) for i in range(3)]
    maxs_p = [max(positions[i::3]) for i in range(3)]
    import_center = [(mins_p[i] + maxs_p[i]) / 2.0 for i in range(3)]
    import_size = [max(maxs_p[i] - mins_p[i], 0.001) for i in range(3)]
    
    orig_center = [orig_unit_base[i] + orig_unit_scale[i] / 2.0 for i in range(3)]
    orig_size = [max(orig_unit_scale[i], 0.001) for i in range(3)]
    
    scale = min(orig_size[i] / import_size[i] for i in range(3))
    
    vbuf = bytearray()
    for vi in range(vert_count):
        px = positions[vi*3]; py = positions[vi*3+1]; pz = positions[vi*3+2]
        tx = (px - import_center[0]) * scale + orig_center[0]
        ty = (py - import_center[1]) * scale + orig_center[1]
        tz = (pz - import_center[2]) * scale + orig_center[2]
        
        ix = round((tx - orig_unit_base[0]) / orig_unit_scale[0] * 32767.0)
        iy = round((ty - orig_unit_base[1]) / orig_unit_scale[1] * 32767.0)
        iz = round((tz - orig_unit_base[2]) / orig_unit_scale[2] * 32767.0)
        ix = max(-32768, min(32767, ix))
        iy = max(-32768, min(32767, iy))
        iz = max(-32768, min(32767, iz))
        
        vbuf += struct.pack('<hhh', ix, iy, iz)
        
        if stride_a >= 12:
            u = uvs[vi*2] if uvs and vi*2 < len(uvs) else 0.0
            v = 1.0 - (uvs[vi*2+1] if uvs and vi*2+1 < len(uvs) else 1.0)
            vbuf += b'\x00\x00'
            try:
                vbuf += struct.pack('<ee', u, v)
            except struct.error:
                vbuf += struct.pack('<ff', u, v)[:4]
        
        remaining = stride_a - (6 + (2 if stride_a >= 12 else 0) + (4 if stride_a >= 12 else 0))
        if remaining > 0:
            vbuf += b'\x00' * remaining
    
    if has_skin:
        stride_b = vdata[7]
        for vi in range(vert_count):
            if skin_indices and vi*4+3 < len(skin_indices):
                vbuf += bytes(skin_indices[vi*4:vi*4+4])
            else:
                vbuf += b'\x00\x00\x00\x00'
            if skin_weights and vi*4+3 < len(skin_weights):
                vbuf += bytes(skin_weights[vi*4:vi*4+4])
            else:
                vbuf += b'\xff\x00\x00\x00'
    elif stride_a >= 18:
        for vi in range(vert_count):
            if skin_indices and vi*4+3 < len(skin_indices):
                for j in range(4):
                    vbuf[vi*stride_a + 10 + j] = skin_indices[vi*4 + j]
                    vbuf[vi*stride_a + 14 + j] = skin_weights[vi*4 + j] if skin_weights and vi*4+j < len(skin_weights) else (255 if j==0 else 0)
    
    ibuf = bytearray()
    for idx in indices:
        ibuf += struct.pack('<H', idx & 0xFFFF)
    
    return bytes(vbuf), bytes(ibuf), vdata


def replace_character_model(char_id, mesh_data):
    """Convert imported mesh data to game format and save to mod workspace.
    
    mesh_data: {
        'positions': [float, ...],
        'uvs': [float, ...] or None,
        'indices': [int, ...],
        'skinIndices': [int, ...] or None,
        'skinWeights': [int, ...] or None,
        'zipFilename': str or None  # override which ZIP to modify
    }
    """
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    if not os.path.isdir(char_dir):
        return None, f'Character directory not found: {char_id}'
    
    zip_files = [f for f in os.listdir(char_dir) if f.endswith('.zip') and not f.startswith('._')]
    if not zip_files:
        return None, 'No zip files found'
    
    positions = mesh_data.get('positions', [])
    uvs = mesh_data.get('uvs', None)
    indices = mesh_data.get('indices', [])
    skin_indices = mesh_data.get('skinIndices', None)
    skin_weights = mesh_data.get('skinWeights', None)
    
    if len(positions) < 9 or len(indices) < 3:
        return None, 'Not enough position or index data'
    
    zip_data = None
    zip_path = None
    for zf_name in zip_files:
        try:
            candidate = load_zip(os.path.join(char_dir, zf_name))
            has_oct = any(n.lower().endswith('.oct') and '/motions/' not in n.lower() for n in candidate)
            if has_oct:
                zip_data = candidate
                zip_path = os.path.join(char_dir, zf_name)
                break
        except Exception:
            continue
    if zip_data is None:
        return None, 'No ZIP with OCT data found'
    
    oct_name = next((n for n in zip_data if n.lower().endswith('.oct') and '/motions/' not in n.lower()), None)
    if not oct_name:
        return None, 'No OCT file found in ZIP'
    
    try:
        stream = bstream.BStream(bytes=zip_data[oct_name])
        obj = Octane(stream)
    except Exception as e:
        return None, f'Failed to parse OCT: {e}'
    
    stnp = obj.get('SceneTreeNodePool', {})
    first_prim = None
    for k in sorted(stnp.keys(), key=lambda x: int(x)):
        node = stnp[k]
        if node.get('Type') in ('Geometry', 'Geometry3d') and 'Primitives' in node:
            prims_obj = node['Primitives']
            for pk in sorted(prims_obj.keys(), key=lambda x: int(x)):
                prim = prims_obj[pk]
                if isinstance(prim, dict):
                    first_prim = prim
                    break
            if first_prim:
                break
    
    if not first_prim:
        return None, 'No geometry primitives found in OCT'
    
    orig_vdata = [int(x) for x in first_prim.get('Vdata', [2, 0, 0, 0, 12, 0, 0, 8])]
    vdata = list(orig_vdata)
    idata = [int(x) for x in first_prim.get('Idata', [0, 0, 0, 0])]
    unit_base = [float(x) for x in first_prim.get('UnitBase', [0, 0, 0])]
    unit_scale = [float(x) for x in first_prim.get('UnitScale', [1, 1, 1])]
    
    vbuf_new, ibuf_new, vdata_out = encode_mesh_to_game_format(
        positions, uvs, indices, orig_vdata, unit_base, unit_scale,
        skin_indices, skin_weights
    )
    vdata = vdata_out
    
    vbuf_fn = None
    ibuf_fn = None
    vpool = obj.get('VertexBufferPool', {})
    for k in vpool:
        fn = vpool[k].get('FileName', '')
        if fn:
            vbuf_fn = fn
            break
    if not vbuf_fn:
        for name in zip_data:
            if name.lower().endswith('.vbuf'):
                vbuf_fn = name
                break
    ipool = obj.get('IndexBufferPool', {})
    for k in ipool:
        fn = ipool[k].get('FileName', '')
        if fn:
            ibuf_fn = fn
            break
    if not ibuf_fn:
        for name in zip_data:
            if name.lower().endswith('.ibuf'):
                ibuf_fn = name
                break
    
    if not vbuf_fn:
        return None, 'Cannot determine vbuf filename'
    
    mod_vbuf_rel = f'characters/{char_id}/{vbuf_fn}'
    mod_ibuf_rel = f'characters/{char_id}/{ibuf_fn}' if ibuf_fn else None
    
    result_vbuf, err = mod_save_file(mod_vbuf_rel, base64.b64encode(vbuf_new).decode())
    if err:
        return None, f'Failed to save vbuf: {err}'
    
    result_ibuf = None
    if ibuf_fn:
        result_ibuf, err = mod_save_file(mod_ibuf_rel, base64.b64encode(ibuf_new).decode())
        if err:
            return None, f'Failed to save ibuf: {err}'
    
    idata_updated = list(idata)
    idata_updated[3] = len(indices)
    mod_meta_rel = f'characters/{char_id}/_model_override.json'
    mod_save_text(mod_meta_rel, json.dumps({
        'vbufName': vbuf_fn,
        'ibufName': ibuf_fn,
        'vdata': vdata,
        'idata': idata_updated,
        'origVdata': list(orig_vdata),
        'origIdata': list(idata),
        'unitBase': unit_base,
        'unitScale': unit_scale,
        'vertCount': len(positions) // 3,
        'triCount': len(indices) // 3,
    }))
    
    return {
        'vbuf': {'path': mod_vbuf_rel, 'size': len(vbuf_new)},
        'ibuf': {'path': mod_ibuf_rel, 'size': len(ibuf_new)} if ibuf_fn else None,
        'vertCount': len(positions) // 3,
        'triCount': len(indices) // 3,
        'zipFilename': zip_files[0],
        'vdata': vdata,
        'unitBase': unit_base,
        'unitScale': unit_scale,
    }, None


def replace_character_texture(char_id, tex_hash, rgba_b64, width, height, fmt='BC3'):
    """Re-encode an edited texture to game format and save to mod workspace."""
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    if not os.path.isdir(char_dir):
        return None, f'Character directory not found: {char_id}'
    
    zip_files = [f for f in os.listdir(char_dir) if f.endswith('.zip') and not f.startswith('._')]
    if not zip_files:
        return None, 'No zip files found'
    
    zip_path = os.path.join(char_dir, zip_files[0])
    try:
        zip_data = load_zip(zip_path)
    except Exception:
        return None, 'Failed to load ZIP'
    
    tbody_name = None
    for name in zip_data:
        name_lower = name.lower()
        if (name_lower.endswith('.tbody') or name_lower.endswith('.dds')) and tex_hash in name:
            tbody_name = name
            break
    
    if not tbody_name:
        for name in zip_data:
            name_lower = name.lower()
            if (name_lower.endswith('.tbody') or name_lower.endswith('.dds')):
                tbody_name = name
                break
    
    if not tbody_name:
        return None, f'No texture file found matching hash {tex_hash}'
    
    rgba_raw = base64.b64decode(rgba_b64)
    
    if fmt == 'BC3':
        encoded = encode_bc3(rgba_raw, width, height)
    elif fmt == 'RGBA8':
        encoded = encode_rgba8(rgba_raw, width, height)
    elif fmt == 'BC1':
        encoded = encode_bc1(rgba_raw, width, height)
    else:
        return None, f'Encoding not supported for {fmt}'
    
    mod_tex_rel = f'characters/{char_id}/{os.path.basename(tbody_name)}'
    result, err = mod_save_file(mod_tex_rel, base64.b64encode(encoded).decode())
    if err:
        return None, f'Failed to save texture: {err}'
    
    return {
        'path': mod_tex_rel,
        'size': len(encoded),
        'format': fmt,
        'hash': tex_hash,
    }, None


def encode_bc1(rgba, width, height):
    """Encode RGBA data to BC1/DXT1 format."""
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    out = bytearray()
    for by in range(blocks_y):
        for bx in range(blocks_x):
            pixels = []
            for py in range(4):
                for px in range(4):
                    ix = (bx * 4 + px)
                    iy = (by * 4 + py)
                    if ix < width and iy < height:
                        pi = (iy * width + ix) * 4
                        pixels.append((rgba[pi], rgba[pi+1], rgba[pi+2], rgba[pi+3]))
                    else:
                        pixels.append((0, 0, 0, 255))
            r = [p[0] for p in pixels]
            g = [p[1] for p in pixels]
            b = [p[2] for p in pixels]
            max_r, min_r = max(r), min(r)
            max_g, min_g = max(g), min(g)
            max_b, min_b = max(b), min(b)
            c0 = ((max_r >> 3) << 11) | ((max_g >> 2) << 5) | (max_b >> 3)
            c1 = ((min_r >> 3) << 11) | ((min_g >> 2) << 5) | (min_b >> 3)
            out += struct.pack('<HH', c0, c1)
            bits = 0
            for pi in range(16):
                dist0 = (r[pi]-max_r)**2 + (g[pi]-max_g)**2 + (b[pi]-max_b)**2
                dist1 = (r[pi]-min_r)**2 + (g[pi]-min_g)**2 + (b[pi]-min_b)**2
                bits |= (1 if dist1 < dist0 else 0) << (pi * 2) if c0 > c1 else (
                    (2 if dist1 < dist0 else 1) << (pi * 2))
            out += struct.pack('<I', bits & 0xFFFFFFFF)
    return bytes(out)


# ── Romfs browser ──

def extract_character_data(char_id, use_workspace=False):
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
    armature_data = None

    for zf_name in zip_files:
        try:
            zip_data = load_zip(os.path.join(char_dir, zf_name))
        except Exception:
            continue

        if use_workspace:
            for fname in list(zip_data.keys()):
                ws_path = mod_workspace_path(f'characters/{char_id}/{os.path.basename(fname)}')
                if ws_path and os.path.isfile(ws_path):
                    try:
                        with open(ws_path, 'rb') as f:
                            zip_data[fname] = f.read()
                    except Exception:
                        pass

        g, t, m, mt = extract_groups_from_zip(zip_data, True)
        if g and not groups:
            groups = g
        tex_list.extend(t)
        mat_list.extend(m)
        mat_tex_map.update(mt)
        if not armature_data:
            armature_data = extract_armature_from_oct(zip_data)

    if not groups:
        return None, 'No renderable geometry found'

    if use_workspace:
        meta_path = mod_workspace_path(f'characters/{char_id}/_model_override.json')
        if meta_path and os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    override = json.load(f)
                ov_vdata = override.get('vdata', [])
                ov_idata = override.get('idata', [])
                if ov_vdata and groups:
                    for grp in groups:
                        for pr in grp.get('primitives', []):
                            if len(ov_vdata) >= 8:
                                pr['vdata'] = list(ov_vdata)
                            if len(ov_idata) >= 4:
                                pr['idata'] = list(ov_idata)
            except Exception:
                pass

    result = {
        'groups': groups,
        'textures': tex_list,
        'materials': mat_list,
        'matTexMap': mat_tex_map,
    }
    if armature_data:
        result['armature'] = armature_data
    return result, None


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

    armature_data = extract_armature_from_oct(zip_data)

    result = {
        'groups': groups,
        'textures': tex_list,
        'materials': mat_list,
        'matTexMap': mat_tex_map,
    }
    if armature_data:
        result['armature'] = armature_data
    return result, None

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
            if path in ('', '/', '/viewer.html', '/index.html'):
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
                use_ws = params.get('workspace', ['0'])[0] == '1'
                data, err = extract_character_data(char_id, use_workspace=use_ws)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(data)

            elif path == '/api/character_animations':
                char_id = params.get('id', [None])[0]
                if not char_id:
                    self.send_json({'error': 'Missing id parameter'}, 400)
                    return
                clips = list_character_animation_clips(char_id)
                if clips is None:
                    self.send_json({'error': 'Character not found'}, 404)
                    return
                self.send_json({'character': char_id, 'clips': clips})

            elif path == '/api/animation':
                char_id = params.get('char', [None])[0]
                clip_name = params.get('clip', [None])[0]
                if not char_id or not clip_name:
                    self.send_json({'error': 'Missing char or clip parameter'}, 400)
                    return
                data, err = extract_animation_data(char_id, clip_name)
                if err:
                    self.send_json({'error': err}, 404)
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
                disasm = lua_decompile_bytecode(raw)
                if not disasm:
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

            elif path == '/api/model/replace':
                data = json.loads(body)
                char_id = data.get('char_id', '')
                if not char_id:
                    self.send_json({'error': 'Missing char_id'}, 400)
                    return
                result, err = replace_character_model(char_id, data)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(result)

            elif path == '/api/model/replace-tex':
                data = json.loads(body)
                char_id = data.get('char_id', '')
                tex_hash = data.get('hash', '')
                rgba_b64 = data.get('rgba', '')
                width = data.get('width', 0)
                height = data.get('height', 0)
                fmt = data.get('format', 'BC3')
                if not char_id or not rgba_b64 or width <= 0 or height <= 0:
                    self.send_json({'error': 'Missing char_id, rgba, width, or height'}, 400)
                    return
                result, err = replace_character_texture(char_id, tex_hash, rgba_b64, width, height, fmt)
                if err:
                    self.send_json({'error': err}, 400)
                    return
                self.send_json(result)

            elif path == '/api/model/workspace-files':
                try:
                    data = json.loads(body) if body else {}
                except Exception:
                    data = {}
                char_id = data.get('char_id', '')
                ws_dir = f'characters/{char_id}' if char_id else ''
                ws_path = mod_workspace_path(ws_dir)
                files = []
                if ws_path and os.path.isdir(ws_path):
                    for f in os.listdir(ws_path):
                        fpath = os.path.join(ws_path, f)
                        if os.path.isfile(fpath):
                            files.append({'name': f, 'size': os.path.getsize(fpath)})
                self.send_json({'char_id': char_id, 'workspaceFiles': files})

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
