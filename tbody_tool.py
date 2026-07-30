#!/usr/bin/env python3
"""
tbody_tool.py — Extract and repack .tbody ↔ .dds/.gnf
for Cars 3: Driven to Win / Avalanche Software Engine.

Modes:
  --extract <file.tbody>              → .dds/.gnf + sidecar .tbody.json
  --inject  <file.dds> <meta.json>    → repack .tbody
  --list-mtb <file.mtb>               → list textures referenced by MTB
  --bundle-extract <bundle.zip>       → extract all textures from bundle

Features:
  • Header stripping / re-stitching (wrapper preserved in JSON)
  • Morton Z-order (PS4 GNF) / GOB (Switch Tegra X1) swizzle & deswizzle
  • 256-byte / 4096-byte alignment & zero-padding
  • JSON sidecar with pitch, flags, surface format, pixel format, wrapper
"""

import os, sys, struct, json, argparse, base64, math, zipfile

# ────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────
DDS_MAGIC = b'DDS '
GNF_MAGIC = b'GNF\x00'
BNDL_MAGIC = b'BNDL'
TBODY_WRAPPER_SIZE = 0x80
DDS_HEADER_SIZE = 128
GNF_HEADER_SIZE = 128

TEXTURE_FORMAT_MAP = {
    0x5E: 'BC3', 0x9E: 'BC3', 0x5C: 'BC1', 0x9C: 'BC1',
    0x9B: 'BC7', 0x5A: 'RGBA8', 0x98: 'BC3',
    0x70: 'BC5', 0xB0: 'BC5', 0x60: 'BC4', 0xA0: 'BC4',
}

BC_BLOCK_SIZES = {'BC1': 8, 'BC4': 8, 'BC3': 16, 'BC5': 16, 'BC7': 16, 'RGBA8': 16}

FOURCC_MAP = {'BC1': b'DXT1', 'BC3': b'DXT5', 'BC4': b'ATI1', 'BC5': b'ATI2', 'BC7': b'BC7\x00'}

REV_FOURCC = {v: k for k, v in FOURCC_MAP.items()}
REV_FOURCC.update({b'DXT3': 'BC3', b'BC4 ': 'BC4', b'BC5 ': 'BC5', b'BC7 ': 'BC7'})

DDS_CAPS_TEXTURE = 0x1000


def fmt_from_fourcc(fourcc):
    return REV_FOURCC.get(fourcc[:4], 'BC3')


def next_pow2(v):
    return 1 << (v - 1).bit_length()


def align_up(v, alignment):
    return (v + alignment - 1) // alignment * alignment


def fmt_code_to_str(raw_fmt):
    return TEXTURE_FORMAT_MAP.get(raw_fmt, f'0x{raw_fmt:04X}')


# ────────────────────────────────────────────────────────────
# Morton Z-order swizzle (PS4 GNF)
# ────────────────────────────────────────────────────────────

def _morton_2d(x, y):
    z = 0
    for i in range(16):
        z |= ((x >> i) & 1) << (2 * i)
        z |= ((y >> i) & 1) << (2 * i + 1)
    return z


def swizzle_morton(data, width, height, block_size):
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    total = bw * bh
    if len(data) < total * block_size:
        return data
    out = bytearray(len(data))
    for by in range(bh):
        for bx in range(bw):
            src = (by * bw + bx) * block_size
            mi = _morton_2d(bx, by) % total
            dst = mi * block_size
            out[dst:dst + block_size] = data[src:src + block_size]
    return bytes(out)


def deswizzle_morton(data, width, height, block_size):
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    total = bw * bh
    if len(data) < total * block_size:
        return data
    out = bytearray(len(data))
    for by in range(bh):
        for bx in range(bw):
            dst = (by * bw + bx) * block_size
            mi = _morton_2d(bx, by) % total
            src = mi * block_size
            out[dst:dst + block_size] = data[src:src + block_size]
    return bytes(out)


# ────────────────────────────────────────────────────────────
# Switch GOB block-linear deswizzle (Tegra X1)
# ────────────────────────────────────────────────────────────

def _gob_offset(x, y):
    return (x // 32) * 256 + (y // 2) * 64 + ((x % 32) // 16) * 32 + (y & 1) * 16 + (x & 15)


def deswizzle_gob(data, width, height, block_size):
    bx = (width + 3) // 4
    by = (height + 3) // 4
    total = bx * by * block_size
    out = bytearray(total)
    gob_h, gob_w = 8, 64
    gob_size = gob_w * gob_h
    bpg = gob_w // block_size
    gx = (bx + bpg - 1) // bpg
    gy = (by + gob_h - 1) // gob_h
    for gob_y in range(gy):
        for gob_x in range(gx):
            gi = gob_y * gx + gob_x
            s_base = gi * gob_size
            lby = gob_y * gob_h
            lbx = gob_x * bpg
            for row in range(gob_h):
                for col in range(gob_w):
                    sx = s_base + _gob_offset(col, row)
                    bir = col // block_size
                    oib = col % block_size
                    byi = lby + row
                    bxi = lbx + bir
                    if bxi < bx and byi < by:
                        lx = lby * bx * block_size + byi * bx * block_size + lbx * block_size + bir * block_size + oib
                        if sx < len(data) and lx < total:
                            out[lx] = data[sx]
    return bytes(out)


def swizzle_gob(data, width, height, block_size):
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    total = bw * bh * block_size
    gob = bytearray(total)
    gob_h, gob_w = 8, 64
    gob_size = gob_w * gob_h
    bpg = gob_w // block_size
    gx = (bw + bpg - 1) // bpg
    gy = (bh + gob_h - 1) // gob_h
    for gob_y in range(gy):
        for gob_x in range(gx):
            gi = gob_y * gx + gob_x
            dsti = gi * gob_size
            lby = gob_y * gob_h
            lbx = gob_x * bpg
            for row in range(gob_h):
                for col in range(gob_w):
                    dsti2 = dsti + _gob_offset(col, row)
                    bir = col // block_size
                    oib = col % block_size
                    byi = lby + row
                    bxi = lbx + bir
                    if bxi < bw and byi < bh:
                        srci = (byi * bw + bxi) * block_size + oib
                        if srci < len(data) and dsti2 < total:
                            gob[dsti2] = data[srci]
    return bytes(gob)


# ────────────────────────────────────────────────────────────
# DDS header helpers
# ────────────────────────────────────────────────────────────

def build_dds_header(width, height, fourcc, data_size):
    hdr = bytearray(128)
    struct.pack_into('<I', hdr, 0, 0x20534444)
    struct.pack_into('<I', hdr, 4, 124)
    struct.pack_into('<I', hdr, 8, 0x1007)
    struct.pack_into('<I', hdr, 12, height)
    struct.pack_into('<I', hdr, 16, width)
    struct.pack_into('<I', hdr, 20, data_size)
    struct.pack_into('<I', hdr, 76, 32)
    struct.pack_into('<I', hdr, 80, 4)
    hdr[84:88] = fourcc[:4]
    struct.pack_into('<I', hdr, 96, DDS_CAPS_TEXTURE)
    return bytes(hdr)


def parse_dds(data):
    if len(data) < 128 or data[:4] != DDS_MAGIC:
        return None
    w = struct.unpack_from('<I', data, 16)[0]
    h = struct.unpack_from('<I', data, 12)[0]
    fc = data[84:88]
    fmt = fmt_from_fourcc(fc)
    bs = BC_BLOCK_SIZES.get(fmt, 16)
    return {'width': w, 'height': h, 'fourcc': fc, 'format': fmt, 'block_size': bs, 'payload': data[128:]}


# ────────────────────────────────────────────────────────────
# Minimal GNF header helpers
# ────────────────────────────────────────────────────────────

def build_gnf_header(width, height, data_format, data_size):
    hdr = bytearray(GNF_HEADER_SIZE)
    hdr[0:4] = GNF_MAGIC
    struct.pack_into('<I', hdr, 8, 0x4000000)
    struct.pack_into('<I', hdr, 12, width)
    struct.pack_into('<I', hdr, 16, height)
    struct.pack_into('<I', hdr, 20, 1)
    struct.pack_into('<I', hdr, 24, 1)
    struct.pack_into('<I', hdr, 36, data_size)
    return bytes(hdr)


# ────────────────────────────────────────────────────────────
# MTB parsing (texture binding metadata)
# ────────────────────────────────────────────────────────────

MTB_ENTRY_SIZE = 16


def parse_mtb(mtb_data):
    """Parse BNDL MTB file, return list of texture entries."""
    entries = []
    if len(mtb_data) < 0x28 or mtb_data[:4] != BNDL_MAGIC:
        return entries
    num_tex = struct.unpack_from('<I', mtb_data, 0x1C)[0]
    for i in range(num_tex):
        base = 0x28 + i * MTB_ENTRY_SIZE
        if base + MTB_ENTRY_SIZE > len(mtb_data):
            break
        hash_bytes = mtb_data[base:base + 8]
        hash_hex = hash_bytes.hex()
        raw_fmt = struct.unpack_from('<H', mtb_data, base + 10)[0]
        b13 = mtb_data[base + 13]
        slot = mtb_data[base + 14]
        entries.append({
            'index': i,
            'hash': hash_hex,
            'hash_bytes': hash_bytes,
            'raw_fmt': raw_fmt,
            'format': fmt_code_to_str(raw_fmt),
            'height': b13,
            'slot': slot,
            'raw': mtb_data[base:base + MTB_ENTRY_SIZE].hex(),
        })
    return entries


def mtb_entries_to_tbody_info(entries, tbody_data_map):
    """Pair MTB entries with tbody data, computing dimensions."""
    results = []
    for e in entries:
        h = e['hash']
        raw = tbody_data_map.get(h)
        if raw is None:
            continue
        fmt = e['format']
        bs = BC_BLOCK_SIZES.get(fmt, 16) if fmt in BC_BLOCK_SIZES else 16
        height = e['height']
        total_pixels = (len(raw) // bs) * 16
        width = total_pixels // height if height > 0 else 0
        results.append({
            'hash': h,
            'width': width,
            'height': height,
            'format': fmt,
            'raw_fmt': e['raw_fmt'],
            'slot': e['slot'],
            'block_size': bs,
            'payload': raw,
            'payload_size': len(raw),
        })
    return results


# ────────────────────────────────────────────────────────────
# TBody parser / extractor (standalone)
# ────────────────────────────────────────────────────────────

def parse_tbody(raw, filename='', known_width=None, known_height=None, known_format=None, known_bs=None):
    info = {
        'original_name': os.path.basename(filename) if filename else '',
        'original_size': len(raw),
        'embedded_type': None,
        'data_offset': 0,
        'payload': None,
        'width': known_width or 0,
        'height': known_height or 0,
        'format': known_format or 'BC3',
        'block_size': known_bs or BC_BLOCK_SIZES.get(known_format, 16) if known_format else 16,
        'is_swizzled': False,
        'swizzle_type': 'none',
        'mip_count': 0,
        'alignment': 256,
        'original_pitch': 0,
        'flags': 0,
        'surface_format': 0,
        'pixel_format': 0,
    }

    # Detect platform from filename
    fn_lower = filename.lower()
    if 'ps4' in fn_lower or 'ps3' in fn_lower or 'orbis' in fn_lower:
        info['platform'] = 'ps4'
    elif 'switch' in fn_lower or 'nx' in fn_lower:
        info['platform'] = 'switch'
    elif 'wii' in fn_lower or 'gtx' in fn_lower:
        info['platform'] = 'wiiu'
    else:
        info['platform'] = 'pc'

    # Save wrapper (everything before DDS/GNF magic, or first 0x80 bytes)
    dds_off = raw.find(DDS_MAGIC)
    gnf_off = raw.find(GNF_MAGIC)

    if dds_off >= 0 and (gnf_off < 0 or dds_off < gnf_off):
        dds_info = parse_dds(raw[dds_off:])
        if dds_info:
            info['embedded_type'] = 'DDS'
            info['data_offset'] = dds_off
            info['wrapper'] = raw[:dds_off]
            info['payload'] = dds_info['payload']
            info['width'] = info['width'] or dds_info['width']
            info['height'] = info['height'] or dds_info['height']
            info['format'] = dds_info['format']
            info['block_size'] = dds_info['block_size']

    elif gnf_off >= 0:
        info['embedded_type'] = 'GNF'
        info['data_offset'] = gnf_off
        info['wrapper'] = raw[:gnf_off]
        payload_offset = gnf_off + GNF_HEADER_SIZE
        info['payload'] = raw[payload_offset:]
        gnf_hdr = raw[gnf_off:gnf_off + GNF_HEADER_SIZE]
        if len(gnf_hdr) >= 24:
            info['width'] = info['width'] or struct.unpack_from('<I', gnf_hdr, 12)[0]
            info['height'] = info['height'] or struct.unpack_from('<I', gnf_hdr, 16)[0]

    else:
        # Try tbody mini-header (w@4, h@6, fmt@8, doff@12)
        if len(raw) >= 16:
            hw = struct.unpack_from('<H', raw, 4)[0]
            hh = struct.unpack_from('<H', raw, 6)[0]
            hfmt = struct.unpack_from('<I', raw, 8)[0]
            hdoff = struct.unpack_from('<I', raw, 12)[0]
            if 0 < hw <= 4096 and 0 < hh <= 4096 and 0 < hdoff <= len(raw):
                info['embedded_type'] = 'TBODY_RAW'
                info['data_offset'] = hdoff
                info['wrapper'] = raw[:hdoff]
                info['payload'] = raw[hdoff:]
                info['width'] = info['width'] or hw
                info['height'] = info['height'] or hh
                str_fmt = TEXTURE_FORMAT_MAP.get(hfmt & 0xFF)
                if str_fmt:
                    info['format'] = str_fmt
                    info['block_size'] = BC_BLOCK_SIZES.get(str_fmt, 16)

        if info['embedded_type'] is None:
            info['embedded_type'] = 'RAW_BCN'
            info['data_offset'] = 0
            info['payload'] = raw
            info['wrapper'] = b''

    # Dimension inference from payload if not known and not already set
    if (info['width'] == 0 or info['height'] == 0) and info['payload']:
        bs = info['block_size']
        n_blocks = len(info['payload']) // bs
        total_pixels = n_blocks * 16
        # Try to find square-ish power-of-2 dimensions
        best = None
        for w in [4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096]:
            if total_pixels % w == 0:
                h = total_pixels // w
                if 4 <= h <= 8192 and h % 4 == 0:
                    ratio = max(w, h) / max(min(w, h), 1)
                    if best is None or ratio < best[0]:
                        best = (ratio, w, h)
        if best:
            info['width'] = info['width'] or best[1]
            info['height'] = info['height'] or best[2]

    return info


# ────────────────────────────────────────────────────────────
# Extract (single file)
# ────────────────────────────────────────────────────────────

def extract(tbody_path, output_dir=None, deswizzle=False,
            force_width=None, force_height=None, force_format=None):
    with open(tbody_path, 'rb') as f:
        raw = f.read()

    info = parse_tbody(raw, tbody_path,
                       known_width=force_width, known_height=force_height,
                       known_format=force_format)

    if not info['payload'] or len(info['payload']) == 0:
        sys.exit(f'Error: No payload data found in {tbody_path}')

    if output_dir is None:
        output_dir = os.path.dirname(tbody_path) or '.'
    os.makedirs(output_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(tbody_path))[0]
    payload = info['payload']

    # Deswizzle
    if deswizzle and info['is_swizzled']:
        if info['swizzle_type'] == 'morton':
            payload = deswizzle_morton(payload, info['width'], info['height'], info['block_size'])
        elif info['swizzle_type'] == 'gob':
            payload = deswizzle_gob(payload, info['width'], info['height'], info['block_size'])

    # Write DDS (or GNF)
    if info['embedded_type'] == 'GNF':
        gnf_hdr = build_gnf_header(info['width'], info['height'], info['format'], len(payload))
        if info['data_offset'] >= 0 and info['data_offset'] + GNF_HEADER_SIZE <= len(raw):
            orig_gnf = raw[info['data_offset']:info['data_offset'] + GNF_HEADER_SIZE]
            if orig_gnf[:4] == GNF_MAGIC:
                gnf_hdr = orig_gnf
        out_data = gnf_hdr + payload
        out_ext = '.gnf'
    else:
        fourcc = FOURCC_MAP.get(info['format'], b'DXT5')
        dds_hdr = build_dds_header(info['width'], info['height'], fourcc, len(payload))
        if info['data_offset'] >= 0 and info['data_offset'] + DDS_HEADER_SIZE <= len(raw):
            orig_dds = raw[info['data_offset']:info['data_offset'] + DDS_HEADER_SIZE]
            if orig_dds[:4] == DDS_MAGIC:
                dds_hdr = orig_dds
        out_data = dds_hdr + payload
        out_ext = '.dds'

    out_path = os.path.join(output_dir, stem + out_ext)
    with open(out_path, 'wb') as f:
        f.write(out_data)

    # JSON sidecar
    meta = {k: v for k, v in info.items()
            if k not in ('payload', 'wrapper')}
    if info.get('wrapper') is not None:
        meta['wrapper_base64'] = base64.b64encode(info['wrapper']).decode()
        meta['wrapper_hex'] = info['wrapper'].hex()
        meta['wrapper_size'] = len(info['wrapper'])
    meta['output_file'] = os.path.basename(out_path)
    meta['payload_size'] = len(info['payload'])
    meta['deswizzled'] = deswizzle and info['is_swizzled']

    json_path = os.path.join(output_dir, stem + '.tbody.json')
    with open(json_path, 'w') as f:
        json.dump(meta, f, indent=2, default=str)

    print(f'[EXTRACT] {out_path}')
    print(f'          {info["width"]}x{info["height"]}  {info["format"]}  payload={len(info["payload"])}B  type={info["embedded_type"]}')
    print(f'[META]    {json_path}')
    if info['is_swizzled']:
        print(f'[SWIZZLE] {info["swizzle_type"]}' + (' → linear' if deswizzle else ' (use --deswizzle)'))

    return out_path, json_path


# ────────────────────────────────────────────────────────────
# Inject / repack
# ────────────────────────────────────────────────────────────

def inject(dds_path, meta_path, output_dir=None, reswizzle=False, alignment=256):
    if not os.path.exists(meta_path):
        sys.exit(f'Error: metadata not found: {meta_path}')
    with open(meta_path) as f:
        meta = json.load(f)

    with open(dds_path, 'rb') as f:
        dds_raw = f.read()

    dds_info = parse_dds(dds_raw)
    if dds_info is None:
        sys.exit(f'Error: {dds_path} is not a valid DDS file')

    payload = dds_info['payload']
    width = dds_info['width']
    height = dds_info['height']
    fmt = dds_info['format']

    # Re-swizzle
    if reswizzle or meta.get('is_swizzled', False):
        sw_type = meta.get('swizzle_type', 'morton')
        bs = BC_BLOCK_SIZES.get(fmt, 16)
        if sw_type == 'morton':
            payload = swizzle_morton(payload, width, height, bs)
        elif sw_type == 'gob':
            payload = swizzle_gob(payload, width, height, bs)

    # Alignment padding
    payload_padded = payload + b'\x00' * (align_up(len(payload), alignment) - len(payload))

    # Reconstruct wrapper
    wrapper_b64 = meta.get('wrapper_base64', '') or meta.get('wrapper_bytes', '') or ''
    wrapper = base64.b64decode(wrapper_b64) if wrapper_b64 else b''
    if not wrapper:
        wrapper = b'\x00' * meta.get('wrapper_size', TBODY_WRAPPER_SIZE)

    tbody_data = wrapper + payload_padded

    if output_dir is None:
        output_dir = os.path.dirname(dds_path) or '.'
    os.makedirs(output_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(meta.get('original_name', dds_path)))[0]
    out_path = os.path.join(output_dir, stem + '.tbody')

    with open(out_path, 'wb') as f:
        f.write(tbody_data)

    print(f'[INJECT]  {out_path}')
    print(f'          source={os.path.basename(dds_path)}  {width}x{height} {fmt}')
    print(f'          wrapper={len(wrapper)}B  payload={len(payload)}B  padded={len(payload_padded)}B  align={alignment}')
    if meta.get('is_swizzled', False):
        print(f'          swizzled={meta.get("swizzle_type", "morton")}')

    return out_path


# ────────────────────────────────────────────────────────────
# List MTB entries
# ────────────────────────────────────────────────────────────

def list_mtb(mtb_path):
    with open(mtb_path, 'rb') as f:
        data = f.read()
    entries = parse_mtb(data)
    if not entries:
        print(f'No valid MTB entries found in {mtb_path}')
        return
    print(f'MTB: {mtb_path}')
    print(f'     {len(entries)} texture(s)')
    print(f'     {"idx":>3}  {"hash":>16}  {"fmt":>8}  {"height":>6}  {"slot":>4}')
    print(f'     {"-"*45}')
    for e in entries:
        print(f'     {e["index"]:>3}  {e["hash"]:>16}  {e["format"]:>8}  {e["height"]:>6}  {e["slot"]:>4}')


# ────────────────────────────────────────────────────────────
# Bundle extract (batch)
# ────────────────────────────────────────────────────────────

def bundle_extract(zip_path, output_dir, deswizzle=False, platform='auto'):
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        mtb_files = [n for n in names if n.endswith('.mtb') and not n.startswith('_')]
        tbody_files = [n for n in names if n.endswith('.tbody')]
        all_tbody = {os.path.splitext(os.path.basename(n))[0]: n for n in tbody_files}

    if not mtb_files:
        # No MTB — extract all tbody files with heuristics
        print(f'No MTB found in {zip_path}, extracting {len(tbody_files)} textures by heuristic')
        with zipfile.ZipFile(zip_path) as z:
            for name in tbody_files:
                raw = z.read(name)
                stem = os.path.splitext(os.path.basename(name))[0]
                out_dir = os.path.join(output_dir, stem)
                os.makedirs(out_dir, exist_ok=True)
                out_file = os.path.join(out_dir, name.replace('/', '_'))
                with open(out_file, 'wb') as f:
                    f.write(raw)
        return

    # Parse first MTB
    with zipfile.ZipFile(zip_path) as z:
        mtb_data = z.read(mtb_files[0])
    entries = parse_mtb(mtb_data)
    if not entries:
        print(f'Could not parse MTB in {zip_path}')
        return

    # Load tbody data
    tbody_map = {}
    with zipfile.ZipFile(zip_path) as z:
        for h, path in all_tbody.items():
            try:
                tbody_map[h] = z.read(path)
            except Exception:
                pass

    # Match with MTB
    with zipfile.ZipFile(zip_path) as z:
        for e in entries:
            h = e['hash']
            if h not in tbody_map:
                continue
            raw = tbody_map[h]
            fmt = e['format']
            bs = BC_BLOCK_SIZES.get(fmt, 16) if fmt in BC_BLOCK_SIZES else 16
            height = e['height']
            total_pixels = (len(raw) // bs) * 16
            width = total_pixels // height if height > 0 else 0

            if width <= 0 or height <= 0:
                continue

            # Determine platform-specific behavior
            is_switch = platform == 'switch' or (platform == 'auto' and ('switch' in zip_path.lower() or 'nx' in zip_path.lower()))
            is_ps4 = platform == 'ps4' or (platform == 'auto' and ('ps4' in zip_path.lower() or 'orbis' in zip_path.lower()))

            payload = raw
            was_deswizzled = False
            sw_type = 'none'
            if is_switch and deswizzle:
                payload = deswizzle_gob(payload, width, height, bs)
                was_deswizzled = True
                sw_type = 'gob'
            elif is_ps4 and deswizzle:
                payload = deswizzle_morton(payload, width, height, bs)
                was_deswizzled = True
                sw_type = 'morton'
            elif is_ps4 or is_switch:
                sw_type = 'gob' if is_switch else 'morton'

            fourcc = FOURCC_MAP.get(fmt, b'DXT5')
            dds_hdr = build_dds_header(width, height, fourcc, len(payload))
            dds_data = dds_hdr + payload

            stem = h
            out_dir = os.path.join(output_dir, stem)
            os.makedirs(out_dir, exist_ok=True)

            dds_path = os.path.join(out_dir, f'{stem}.dds')
            with open(dds_path, 'wb') as f:
                f.write(dds_data)

            json_path = os.path.join(out_dir, f'{stem}.tbody.json')
            meta = {
                'original_name': f'{h}.tbody',
                'original_size': len(raw),
                'embedded_type': 'RAW_BCN',
                'width': width, 'height': height,
                'format': fmt, 'block_size': bs,
                'payload_size': len(payload),
                'is_swizzled': is_ps4 or is_switch,
                'swizzle_type': sw_type,
                'deswizzled': was_deswizzled,
                'wrapper_base64': '',
                'wrapper_size': 0,
                'slot': e['slot'],
                'raw_fmt': e['raw_fmt'],
                'alignment': 256,
            }
            with open(json_path, 'w') as f:
                json.dump(meta, f, indent=2)

            print(f'[EXTRACT] {dds_path}  ({width}x{height}  {fmt}  payload={len(payload)}B){" deswizzled" if was_deswizzled else ""}')

    print(f'Done: {len([e for e in entries if e["hash"] in tbody_map])} textures from {os.path.basename(zip_path)}')


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Convert .tbody ↔ .dds/.gnf for Cars 3: Driven to Win (Avalanche Engine)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single file
  python tbody_tool.py --extract tex.tbody
  python tbody_tool.py --extract tex.tbody --deswizzle
  python tbody_tool.py --extract tex.tbody --width 256 --height 256 --format BC3
  python tbody_tool.py --inject edited.dds tex.tbody.json
  python tbody_tool.py --inject edited.dds tex.tbody.json --reswizzle

  # Batch / MTB
  python tbody_tool.py --list-mtb bundle.mtb
  python tbody_tool.py --bundle-extract bundle.zip --deswizzle
  python tbody_tool.py --bundle-extract bundle.zip --platform switch --deswizzle
""",
    )

    ap.add_argument('--extract', metavar='FILE.tbody', help='Extract .tbody → .dds/.gnf + sidecar .json')
    ap.add_argument('--inject', nargs=2, metavar=('FILE.dds', 'META.json'), help='Repack DDS → .tbody using sidecar')
    ap.add_argument('--list-mtb', metavar='FILE.mtb', help='List texture entries in MTB binding file')
    ap.add_argument('--bundle-extract', metavar='BUNDLE.zip', help='Extract all textures from a bundle ZIP')
    ap.add_argument('--dir', metavar='DIR', default=None, help='Output directory')
    ap.add_argument('--deswizzle', action='store_true', help='Deswizzle on extract')
    ap.add_argument('--reswizzle', action='store_true', help='Re-swizzle on inject')
    ap.add_argument('--width', type=int, default=None, help='Force width (px)')
    ap.add_argument('--height', type=int, default=None, help='Force height (px)')
    ap.add_argument('--format', default=None, choices=['BC1', 'BC3', 'BC4', 'BC5', 'BC7', 'RGBA8'], help='Force texture format')
    ap.add_argument('--alignment', type=int, default=256, help='Payload alignment bytes (default: 256)')
    ap.add_argument('--platform', default='auto', choices=['pc', 'ps4', 'switch', 'auto'], help='Target platform')

    args = ap.parse_args()

    if not any([args.extract, args.inject, args.list_mtb, args.bundle_extract]):
        ap.print_help()
        sys.exit(1)

    if args.list_mtb:
        list_mtb(args.list_mtb)
        return

    if args.bundle_extract:
        bundle_extract(args.bundle_extract, args.dir or os.path.splitext(args.bundle_extract)[0] + '_extracted',
                       deswizzle=args.deswizzle, platform=args.platform)
        return

    if args.extract:
        extract(args.extract, args.dir, deswizzle=args.deswizzle,
                force_width=args.width, force_height=args.height, force_format=args.format)

    if args.inject:
        dds_path, meta_path = args.inject
        inject(dds_path, meta_path, args.dir, reswizzle=args.reswizzle, alignment=args.alignment)


if __name__ == '__main__':
    main()
