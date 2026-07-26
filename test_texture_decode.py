#!/usr/bin/env python3
"""Standalone diagnostic: examine raw .tbody bytes and test BC3 decode math.
Does NOT import world_loader/revoctane."""
import sys, os, struct, base64, zipfile, hashlib, re
from collections import Counter
from PIL import Image
import io

TEXTURE_FORMATS = {0x5E: 'BC3', 0x9E: 'BC3', 0x5C: 'BC1', 0x9C: 'BC1', 0x9B: 'BC7', 0x5A: 'RGBA8', 0x98: 'BC3', 0x70: 'BC5', 0xB0: 'BC5', 0x60: 'BC4', 0xA0: 'BC4'}

def hexdump(data, offset=0, length=64):
    lines = []
    for i in range(0, min(length, len(data)), 16):
        chunk = data[offset+i:offset+i+16]
        hex_str = ' '.join(f'{b:02x}' for b in chunk)
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'  {offset+i:04x}: {hex_str:<48s} {ascii_str}')
    return '\n'.join(lines)

def decode_bc3(raw_data, width, height):
    pixels = bytearray(width * height * 4)
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            block_idx = (by // 4) * ((width + 3) // 4) + (bx // 4)
            boff = block_idx * 16
            if boff + 16 > len(raw_data): continue
            block = raw_data[boff:boff+16]
            a0, a1 = block[0], block[1]
            a_bits = int.from_bytes(block[2:8], 'little')
            c0 = struct.unpack_from('<H', block, 8)[0]
            c1 = struct.unpack_from('<H', block, 10)[0]
            c_bits = struct.unpack_from('<I', block, 12)[0]
            r0 = ((c0>>11)&0x1F)*255//31; g0 = ((c0>>5)&0x3F)*255//63; b0 = (c0&0x1F)*255//31
            r1 = ((c1>>11)&0x1F)*255//31; g1 = ((c1>>5)&0x3F)*255//63; b1 = (c1&0x1F)*255//31
            if a0 > a1:
                alphas = [a0,a1,(6*a0+1*a1)//7,(5*a0+2*a1)//7,(4*a0+3*a1)//7,(3*a0+4*a1)//7,(2*a0+5*a1)//7,(1*a0+6*a1)//7]
            else:
                alphas = [a0,a1,(4*a0+1*a1)//5,(3*a0+2*a1)//5,(2*a0+3*a1)//5,(1*a0+4*a1)//5,0,255]
            if c0 > c1:
                colors = [(r0,g0,b0),(r1,g1,b1),((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3),((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3)]
            else:
                colors = [(r0,g0,b0),(r1,g1,b1),((r0+r1)//2,(g0+g1)//2,(b0+b1)//2),(0,0,0)]
            for py in range(4):
                for px in range(4):
                    x, y = bx + px, by + py
                    if x >= width or y >= height: continue
                    pi = (y*width+x)*4
                    ai = (a_bits>>((py*4+px)*3))&0x7
                    ci = (c_bits>>((py*4+px)*2))&0x3
                    pixels[pi+3] = alphas[ai]
                    pixels[pi:pi+3] = colors[ci]
    return bytes(pixels)

def decode_bc1(raw_data, width, height):
    pixels = bytearray(width * height * 4)
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
                colors=[(r0,g0,b0),(r1,g1,b1),((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3),((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3)]
            else:
                colors=[(r0,g0,b0),(r1,g1,b1),((r0+r1)//2,(g0+g1)//2,(b0+b1)//2),(0,0,0)]
            for py in range(4):
                for px in range(4):
                    x,y = bx+px, by+py
                    if x>=width or y>=height: continue
                    pi = (y*width+x)*4
                    ci = (bits>>((py*4+px)*2))&0x3
                    pixels[pi:pi+3]=colors[ci]; pixels[pi+3]=0 if (c0<=c1 and ci==3) else 255
    return bytes(pixels)

def compute_texture_dimensions(total_data_size, block_size=16):
    total_blocks = total_data_size // block_size
    if total_blocks < 1: return None, None
    candidates = []
    for w in [4,8,16,32,64,128,256,512,1024,2048,4096]:
        bw = (w+3)//4
        if total_blocks % bw != 0: continue
        h = (total_blocks // bw) * 4
        if h < 4 or h > 8192: continue
        ratio = max(w,h) / max(min(w,h),1)
        candidates.append((ratio, w, h))
    for h in [4,8,16,32,64,128,256,512,1024,2048,4096]:
        bh = (h+3)//4
        if total_blocks % bh != 0: continue
        w = (total_blocks // bh) * 4
        if w < 4 or w > 8192: continue
        ratio = max(w,h) / max(min(w,h),1)
        candidates.append((ratio, w, h))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]
    return None, None

def parse_mtb_standalone(mtb_data, zip_names):
    textures = []
    if len(mtb_data) < 0x28: return textures
    num_tex = struct.unpack_from('<I', mtb_data, 0x1C)[0]
    for i in range(num_tex):
        base = 0x28 + i * 16
        if base + 16 > len(mtb_data): break
        hash_hex = mtb_data[base:base+8].hex()
        raw_fmt = struct.unpack_from('<H', mtb_data, base+10)[0]
        b13 = mtb_data[base+13]
        slot = mtb_data[base+14]
        tbody_name = next((name for name in zip_names if name.endswith(f'{hash_hex}.tbody')), None)
        fmt_code = TEXTURE_FORMATS.get(raw_fmt, 'UNKNOWN')
        textures.append({
            'idx': i, 'hash': hash_hex, 'fmt_code': fmt_code, 'raw_fmt': raw_fmt,
            'b13': b13, 'slot': slot, 'tbody_name': tbody_name
        })
    return textures

def analyze_zip(path):
    print(f"\n{'='*80}")
    print(f"ANALYZING: {path}")
    print(f"{'='*80}")

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        mtb_files = [n for n in names if n.lower().endswith('.mtb')]
        tbody_files = [n for n in names if n.lower().endswith('.tbody')]
        print(f"Files: {len(names)} total, {len(mtb_files)} MTB, {len(tbody_files)} TBody")

        if not mtb_files:
            print("No MTB found!")
            return

        mtb_data = zf.read(mtb_files[0])
        print(f"\nMTB file: {mtb_files[0]} ({len(mtb_data)} bytes)")
        print(f"MTB header:")
        print(hexdump(mtb_data, 0, 64))

        # Check for BNDL magic
        if mtb_data[:4] == b'BNDL':
            print(f"  Magic: BNDL (good)")
        else:
            print(f"  Magic: {mtb_data[:4]} (unexpected!)")

        num_tex = struct.unpack_from('<I', mtb_data, 0x1C)[0]
        print(f"  Num textures: {num_tex}")

        textures = parse_mtb_standalone(mtb_data, names)
        print(f"\nParsed {len(textures)} texture entries")

        for t in textures:
            tbody_name = t['tbody_name']
            tbody_data = zf.read(tbody_name) if tbody_name else None
            tbody_size = len(tbody_data) if tbody_data else 0

            print(f"\n  [{t['idx']:2d}] hash={t['hash'][:16]}..  fmt=0x{t['raw_fmt']:02x}({t['fmt_code']:5s})  b13={t['b13']:3d}  slot={t['slot']}  tbody={'YES' if tbody_data else 'NO'}({tbody_size}B)")

            if not tbody_data:
                continue

            # Analyze the raw data
            print(f"       First 32 bytes: {tbody_data[:32].hex()}")

            # Check expected sizes for BC3 at various dimensions
            if t['b13'] > 0:
                blocks_per_col = (t['b13'] + 3) // 4
                total_blocks = tbody_size // 16
                if blocks_per_col > 0 and total_blocks % blocks_per_col == 0:
                    blocks_per_row = total_blocks // blocks_per_col
                    width_from_b13 = blocks_per_row * 4
                    print(f"       b13-based: {width_from_b13}x{t['b13']} (blocks_per_col={blocks_per_col}, blocks_per_row={blocks_per_row})")
                else:
                    print(f"       b13={t['b13']}: does NOT divide evenly! blocks_per_col={blocks_per_col}, total_blocks={total_blocks}")

            # Compute dimensions
            w_bc3, h_bc3 = compute_texture_dimensions(tbody_size, 16)
            w_bc1, h_bc1 = compute_texture_dimensions(tbody_size, 8)
            print(f"       BC3 dimensions: {w_bc3}x{h_bc3}")
            print(f"       BC1 dimensions: {w_bc1}x{h_bc1}")

            # Try decoding with various possible dimensions
            if t['b13'] > 0:
                blocks_per_col = (t['b13'] + 3) // 4
                total_blocks = tbody_size // 16
                if blocks_per_col > 0 and total_blocks % blocks_per_col == 0:
                    blocks_per_row = total_blocks // blocks_per_col
                    w = blocks_per_row * 4
                    h = t['b13']

                    print(f"\n       === Decoding as {t['fmt_code']} {w}x{h} ===")

                    if t['fmt_code'] == 'BC3':
                        rgba = decode_bc3(tbody_data, w, h)
                        img = Image.frombytes('RGBA', (w, h), rgba)
                    elif t['fmt_code'] == 'BC1':
                        rgba = decode_bc1(tbody_data, w, h)
                        img = Image.frombytes('RGBA', (w, h), rgba)
                    else:
                        print(f"       Skipping decode for {t['fmt_code']}")
                        continue

                    # Analyze the decoded image
                    pixels = list(img.getdata())
                    alpha_vals = [p[3] for p in pixels]
                    unique_alpha = len(set(alpha_vals))
                    avg_alpha = sum(alpha_vals) / len(alpha_vals)
                    avg_r = sum(p[0] for p in pixels) / len(pixels)
                    avg_g = sum(p[1] for p in pixels) / len(pixels)
                    avg_b = sum(p[2] for p in pixels) / len(pixels)

                    # Check if image is all-black/all-pink
                    black_count = sum(1 for p in pixels if p[0] < 10 and p[1] < 10 and p[2] < 10)
                    pink_count = sum(1 for p in pixels if p[0] > 240 and p[1] < 20 and p[2] > 240)
                    total_pixels = len(pixels)

                    print(f"       Avg RGBA: ({avg_r:.0f}, {avg_g:.0f}, {avg_b:.0f}, {avg_alpha:.0f})")
                    print(f"       Alpha: {unique_alpha} unique, avg={avg_alpha:.1f}")
                    print(f"       Black pixels: {black_count}/{total_pixels} ({100*black_count/total_pixels:.1f}%)")
                    print(f"       Pink pixels: {pink_count}/{total_pixels} ({100*pink_count/total_pixels:.1f}%)")

                    # Top 5 colors
                    color_counts = Counter(pixels).most_common(5)
                    print(f"       Top 5 colors: {color_counts}")

                    # Sample specific pixels
                    print(f"       Corner pixels: (0,0)={img.getpixel((0,0))}  (1,0)={img.getpixel((1,0))}  (0,1)={img.getpixel((0,1))}")
                    if w > 4 and h > 4:
                        print(f"       Mid pixels: (4,4)={img.getpixel((4,4))}  (8,8)={'N/A' if w<=8 else img.getpixel((8,8))}")

                    # Save for visual inspection
                    save_path = f'/tmp/test_decode_{t["idx"]}_{t["hash"][:8]}.png'
                    img.save(save_path)
                    print(f"       Saved: {save_path}")

            # Also test if this data could be a different format
            # Check byte patterns
            first_8 = tbody_data[:8]
            byte_counts = Counter(first_8)
            print(f"       First 8 bytes distribution: {dict(byte_counts.most_common(4))}")

if __name__ == '__main__':
    paths = [
        '/Volumes/cars3MTW/Cars3mtw/romfs/assets/characters/cars3_arvy/cars3_arvy.zip',
    ]
    track_dir = '/Volumes/cars3MTW/Cars3mtw/romfs/assets/env_assets/cars3'
    if os.path.isdir(track_dir):
        for root, dirs, files in os.walk(track_dir):
            for f in sorted(files):
                if f.endswith('.zip') and not f.startswith('.'):
                    paths.append(os.path.join(root, f))
                    if len(paths) > 5: break
            if len(paths) > 5: break

    for p in paths:
        if os.path.exists(p):
            analyze_zip(p)
        else:
            print(f"Not found: {p}")
