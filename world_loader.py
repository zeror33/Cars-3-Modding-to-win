import os, struct, zipfile, bstream, hashlib, base64, re
from revoctane import Octane

def load_zip(path):
    data = {}
    with zipfile.ZipFile(path, 'r') as z:
        for name in z.namelist():
            try: data[name] = z.read(name)
            except: pass
    return data

def identity():
    return [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]

def transform_point(m, p):
    x, y, z = p
    return (
        m[0]*x + m[4]*y + m[8]*z + m[12],
        m[1]*x + m[5]*y + m[9]*z + m[13],
        m[2]*x + m[6]*y + m[10]*z + m[14],
    )

def decode_vbuf(vbuf, prim, world_mat):
    vdata = prim['vdata']; idata = prim['idata']
    vert_count = vdata[1] if len(vdata) > 1 else 0
    offset_a = vdata[3] if len(vdata) > 3 else 0
    stride_a = vdata[4] if len(vdata) > 4 else 12
    byte_offset = idata[1] if len(idata) > 1 else 0
    base_index = idata[2] if len(idata) > 2 else 0
    index_count = idata[3] if len(idata) > 3 else 0
    ub = prim['unitBase']; us = prim['unitScale']
    pos, uvs, idx = [], [], []
    if vert_count > 0 and stride_a > 0:
        for i in range(vert_count):
            off = offset_a + i * stride_a
            if off + 6 > len(vbuf): break
            x = struct.unpack_from('<h', vbuf, off)[0]
            y = struct.unpack_from('<h', vbuf, off+2)[0]
            z = struct.unpack_from('<h', vbuf, off+4)[0]
            px = ub[0] + (x/32767.0)*us[0]
            py = ub[1] + (y/32767.0)*us[1]
            pz = ub[2] + (z/32767.0)*us[2]
            if world_mat:
                px, py, pz = transform_point(world_mat, (px, py, pz))
            pos.extend([px, py, pz])
            if stride_a >= 12:
                uv_off = off + 8
                if uv_off + 4 <= len(vbuf):
                    uvs.append([struct.unpack_from('<e', vbuf, uv_off)[0], 1.0 - struct.unpack_from('<e', vbuf, uv_off+2)[0]])
    ibuf = prim.get('ibuf', b'')
    if ibuf and index_count > 0:
        for i in range(index_count):
            off = byte_offset + i * 2
            if off + 2 > len(ibuf): break
            idx.append(struct.unpack_from('<H', ibuf, off)[0] - base_index)
    return pos, uvs, idx

def extract_asset_geometry(zip_path, mat_offset=0):
    """Extract geometry primitives from an env_asset ZIP."""
    zip_data = load_zip(zip_path)
    oct_name = next((n for n in zip_data if n.lower().endswith('.oct')), None)
    if not oct_name: return [], {}
    stream = bstream.BStream(bytes=zip_data[oct_name])
    obj = Octane(stream)
    stnp = obj.get('SceneTreeNodePool', {})
    vbuf_name = next((n for n in zip_data if '.vbuf' in n), None)
    ibuf_name = next((n for n in zip_data if '.ibuf' in n), None)
    vbuf = zip_data.get(vbuf_name, b'') if vbuf_name else b''
    ibuf = zip_data.get(ibuf_name, b'') if ibuf_name else b''
    mp = obj.get('MaterialPool', {})
    mat_names = {}
    for k in sorted(mp.keys(), key=lambda x: int(x)):
        mat_names[int(k)] = mp[k].get('Name', mp[k].get('FileName', ''))
    meshes = []
    skip_kw = ['lod1','lod2','lod3','_lod','shadow','proxy','collision','_dmg','damage','chassis_b','chassis_lod']
    for k, v in stnp.items():
        if v.get('Type') != 'Geometry': continue
        node_name = v.get('NodeName', k)
        if any(kw in node_name.lower() for kw in skip_kw): continue
        # Simple identity for now, will need to extract actual matrix
        world_mat = v.get('LocalToParentMatrix', identity())
        prims = v.get('Primitives', {})
        for pk in sorted(prims.keys(), key=lambda x: int(x)):
            prim = prims[pk]
            if not isinstance(prim, dict): continue
            mat_name = prim.get('MaterialName', 'unknown')
            if 'shadowcaster' in mat_name.lower(): continue
            vdata = [int(x) for x in prim.get('Vdata', [2,0,0,0,12,0,0,8])]
            idata = [int(x) for x in prim.get('Idata', [0,0,0,0])]
            prim_data = {
                'vdata': vdata, 'idata': idata,
                'unitBase': [float(x) for x in prim.get('UnitBase',[0,0,0])],
                'unitScale': [float(x) for x in prim.get('UnitScale',[1,1,1])],
                'ibuf': ibuf,
            }
            pos, uvs, indices = decode_vbuf(vbuf, prim_data, world_mat)
            if len(pos) < 9 or len(indices) < 3: continue
            mat_ref = int(prim.get('MaterialReference', -1)) + mat_offset
            meshes.append({
                'positions': pos, 'uvs': uvs, 'indices': indices,
                'materialRef': mat_ref,
                'matName': mat_names.get(mat_ref - mat_offset, mat_name),
            })
    local_mat_pool = {}
    for k in sorted(mp.keys(), key=lambda x: int(x)):
        mname = mp[k].get('Name', mp[k].get('FileName', ''))
        local_mat_pool[int(k)] = mname
    return meshes, local_mat_pool

def register_material_pool(local_pool, all_materials, local_mtm=None):
    """Register a local material pool, return mapping {local_idx: global_idx}. Merges local_mtm into returned dict if given."""
    mapping = {}
    merged_mtm = {}
    for local_idx in sorted(local_pool.keys()):
        mname = local_pool[local_idx]
        if not mname:
            mapping[local_idx] = local_idx
            continue
        existing = next((m for m in all_materials if m['name'] == mname), None)
        if existing:
            mapping[local_idx] = existing['index']
        else:
            new_idx = (max((m['index'] for m in all_materials), default=-1) + 1)
            all_materials.append({'index': new_idx, 'name': mname})
            mapping[local_idx] = new_idx
        # Carry over local_mtm entries remapped to global index
        if local_mtm is not None:
            global_idx = mapping[local_idx]
            for lookup in (str(local_idx), local_idx):
                if lookup in local_mtm:
                    merged_mtm[global_idx] = local_mtm[lookup]
                    del local_mtm[lookup]
                    break
    return mapping, merged_mtm

def gen_track_surface(tnp, tlp, world_mat):
    verts, uvs, idx = [], [], []
    vi = 0; track_u = 0
    adj = {}
    for link in tlp.values():
        n1, n2 = link['Node1Index'], link['Node2Index']
        adj.setdefault(n1, []).append(n2)
        adj.setdefault(n2, []).append(n1)
    visited = set()
    stack = [(0, None)]
    while stack:
        current, prev = stack.pop()
        for next_n in adj.get(current, []):
            if (current, next_n) in visited or (next_n, current) in visited:
                continue
            visited.add((current, next_n))
            n1, n2 = str(current), str(next_n)
            if n1 not in tnp or n2 not in tnp: continue
            p1 = tnp[n1]['Position']; p2 = tnp[n2]['Position']
            w1 = tnp[n1].get('Width', 31.37); w2 = tnp[n2].get('Width', 31.37)
            p1 = transform_point(world_mat, p1); p2 = transform_point(world_mat, p2)
            dx = p2[0] - p1[0]; dz = p2[2] - p1[2]
            length = math.sqrt(dx*dx + dz*dz)
            if length >= 0.01:
                dx /= length; dz /= length; lx, lz = -dz, dx
                p1l = (p1[0] + lx*w1/2, p1[1], p1[2] + lz*w1/2)
                p1r = (p1[0] - lx*w1/2, p1[1], p1[2] - lz*w1/2)
                p2l = (p2[0] + lx*w2/2, p2[1], p2[2] + lz*w2/2)
                p2r = (p2[0] - lx*w2/2, p2[1], p2[2] - lz*w2/2)
                verts.extend([*p1l, *p1r, *p2l, *p2r])
                uvs.append([track_u, 0]); uvs.append([track_u, 1])
                uvs.append([track_u + length, 0]); uvs.append([track_u + length, 1])
                track_u += length
                idx.extend([vi, vi+1, vi+2, vi+1, vi+3, vi+2])
                vi += 4
            stack.append((next_n, current))
    return verts, uvs, idx

def extract_world_full(zip_paths, romfs_dir, env_assets_dir, env_index):
    all_meshes = []; all_tex = []; all_materials = []; mat_tex_map = {}
    seen_tex = set()
    track_mat_idx = None

    # First pass: find the global track matrix (polySurface1 with identity rotation)
    global_track_mat = None
    for zn in zip_paths:
        path = os.path.join(romfs_dir, zn)
        if not os.path.isfile(path): continue
        zip_data = load_zip(path)
        oct_name = next((n for n in zip_data if n.lower().endswith('.oct')), None)
        if not oct_name: continue
        stream = bstream.BStream(bytes=zip_data[oct_name])
        obj = Octane(stream)
        stnp = obj.get('SceneTreeNodePool', {})
        for k, v in stnp.items():
            if v.get('Type') == 'Geometry':
                node_name = v.get('NodeName', k)
                if 'polySurface1' in node_name:
                    world_mat = v.get('LocalToParentMatrix', identity())
                    if world_mat[0] == 1 and world_mat[5] == 1 and world_mat[10] == 1:
                        global_track_mat = world_mat
                        break
        if global_track_mat: break
    
    # 1) Process static geometry and collect placements from world ZIPs
    placements = []
    master_render_mat = None
    for zn in zip_paths:
        path = os.path.join(romfs_dir, zn)
        if not os.path.isfile(path): continue
        zip_data = load_zip(path)
        oct_name = next((n for n in zip_data if n.lower().endswith('.oct')), None)
        if not oct_name: continue
        stream = bstream.BStream(bytes=zip_data[oct_name])
        obj = Octane(stream)
        stnp = obj.get('SceneTreeNodePool', {})
        vbuf_name = next((n for n in zip_data if '.vbuf' in n), None)
        ibuf_name = next((n for n in zip_data if '.ibuf' in n), None)
        vbuf = zip_data.get(vbuf_name, b'') if vbuf_name else b''
        ibuf = zip_data.get(ibuf_name, b'') if ibuf_name else b''
        mp = obj.get('MaterialPool', {})
        
        world_meshes = []
        for k, v in stnp.items():
            if v.get('Type') == 'Geometry':
                node_name = v.get('NodeName', k)
                skip_kw = ['lod1','lod2','lod3','_lod','shadow','proxy','collision','_dmg','damage','chassis_b','chassis_lod']
                if any(kw in node_name.lower() for kw in skip_kw): continue
                world_mat = v.get('LocalToParentMatrix', identity())
                prims = v.get('Primitives', {})
                for pk in sorted(prims.keys(), key=lambda x: int(x)):
                    prim = prims[pk]
                    if not isinstance(prim, dict): continue
                    mat_name = prim.get('MaterialName', 'unknown')
                    if 'shadowcaster' in mat_name.lower(): continue
                    vdata = [int(x) for x in prim.get('Vdata', [2,0,0,0,12,0,0,8])]
                    idata = [int(x) for x in prim.get('Idata', [0,0,0,0])]
                    prim_data = {
                        'vdata': vdata, 'idata': idata,
                        'unitBase': [float(x) for x in prim.get('UnitBase',[0,0,0])],
                        'unitScale': [float(x) for x in prim.get('UnitScale',[1,1,1])],
                        'ibuf': ibuf,
                    }
                    pos, uvs, indices = decode_vbuf(vbuf, prim_data, world_mat)
                    if len(pos) < 9 or len(indices) < 3: continue
                    mat_ref = int(prim.get('MaterialReference', -1))
                    world_meshes.append({
                        'positions': pos, 'uvs': uvs, 'indices': indices,
                        'materialRef': mat_ref, 'name': f'{node_name}_{pk}',
                        'category': 'world',
                    })
            elif v.get('Type') == 'Transform':
                nn = v.get('NodeName', '')
                if nn and nn != 'Layout' and not nn.startswith('TRIG_'):
                    mat = v.get('LocalToParentMatrix', None)
                    placements.append((nn, mat))
                    if 'MasterRender' in nn:
                        master_render_mat = mat

        local_mtm = {}
        mtb_name = next((n for n in zip_data if n.lower().endswith('.mtb')), None)
        if mtb_name:
            for t in parse_mtb_textures(zip_data, zip_data[mtb_name]):
                if t['hash'] not in seen_tex:
                    seen_tex.add(t['hash']); all_tex.append(t)
            local_mtm = parse_matp(zip_data[mtb_name]) or {}
        local_pool = {int(k): mp[k].get('Name', mp[k].get('FileName', '')) for k in mp.keys()}
        mat_map, remapped_mtm = register_material_pool(local_pool, all_materials, local_mtm)
        mat_tex_map.update(remapped_mtm)
        for gm in world_meshes:
            gm['materialRef'] = mat_map.get(gm['materialRef'], -1)
        all_meshes.extend(world_meshes)

        tnp = obj.get('TrackNodePool', {})
        tlp = obj.get('TrackLinkPool', {})
        if tnp and tlp and track_mat_idx is None:
            use_mat = global_track_mat if global_track_mat else identity()
            tpos, tuvs, tidx = gen_track_surface(tnp, tlp, use_mat)
            if tpos:
                all_meshes.append({
                    'positions': tpos, 'uvs': tuvs, 'indices': tidx,
                    'materialRef': -1, 'name': '_track_surface',
                    'category': 'track',
                })
                track_mat_idx = len(all_meshes) - 1

    return all_meshes, all_tex, all_materials, mat_tex_map, track_mat_idx

def compute_texture_dimensions(data_size, block_size):
    if data_size < block_size: return None, None
    num_blocks = data_size // block_size
    total_pixels = num_blocks * 16
    candidates = []
    for w in [256, 512, 128, 1024, 64, 2048, 32, 4096, 16, 8, 4]:
        if total_pixels % w != 0: continue
        h = total_pixels // w
        if h % 4 != 0 or h < 4 or h > 8192: continue
        ratio = max(w, h) / max(min(w, h), 1)
        candidates.append((ratio, w, h))
    for h in [256, 512, 128, 1024, 64, 2048, 32, 4096, 16, 8, 4]:
        if total_pixels % h != 0: continue
        w = total_pixels // h
        if w % 4 == 0 and 4 <= w <= 8192:
            ratio = max(w, h) / max(min(w, h), 1)
            candidates.append((ratio, w, h))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]
    return None, None

TEXTURE_FORMATS = {0x5E: 'BC3', 0x9E: 'BC3', 0x5C: 'BC1', 0x9C: 'BC1', 0x9B: 'BC7', 0x5A: 'RGBA8', 0x98: 'BC3', 0x70: 'BC5', 0xB0: 'BC5', 0x60: 'BC4', 0xA0: 'BC4'}

def parse_mtb_textures(zip_data, mtb_data):
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
        tbody_name = next((name for name in zip_data if name.endswith(f'{hash_hex}.tbody')), None)
        if not tbody_name: continue
        tbody_data = zip_data[tbody_name]
        if len(tbody_data) < 16: continue
        fmt_code = TEXTURE_FORMATS.get(raw_fmt, 'BC3')
        block_size = 8 if fmt_code in ('BC1', 'BC4') else 16

        width, height = None, None
        if b13 > 0:
            blocks_per_col = (b13 + 3) // 4
            total_blocks = len(tbody_data) // block_size
            if blocks_per_col > 0 and total_blocks % blocks_per_col == 0:
                blocks_per_row = total_blocks // blocks_per_col
                width = blocks_per_row * 4
                height = blocks_per_col * 4

        if width is None:
            width, height = compute_texture_dimensions(len(tbody_data), block_size)
        if width is None: continue
        textures.append({
            'hash': hash_hex, 'width': width, 'height': height, 'size': len(tbody_data),
            'format': fmt_code, 'slot': slot, 'mtbIndex': i,
            'data': base64.b64encode(tbody_data).decode(),
        })
    return textures

def parse_matp(mtb_data):
    matp_off = mtb_data.find(b'MATP')
    if matp_off < 0: return {}
    off = matp_off + 4
    u1, u2, num_mat, num_prop = struct.unpack_from('<IIII', mtb_data, off)
    off += 16 + num_prop * 32
    uuid_re = re.compile(rb'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    last_uuid_end = off
    for _ in range(num_mat):
        m = uuid_re.search(mtb_data, last_uuid_end)
        if not m: break
        last_uuid_end = m.end() + 1
    scan_start = last_uuid_end
    mat_to_prop = None
    for try_off in range(scan_start, min(scan_start + 32, len(mtb_data) - num_mat * 2)):
        if try_off % 2 != 0: continue
        vals = []
        for j in range(num_mat): vals.append(struct.unpack_from('<H', mtb_data, try_off + j * 2)[0])
        if all(v < num_prop for v in vals):
            mat_to_prop = vals; break
    if mat_to_prop is None: return {}
    
    prop_tex_lists = []
    i = (off + 3) & ~3
    current_cluster = []
    last_ref_pos = -200
    while i + 8 <= min(len(mtb_data), matp_off + 20 + u2):
        val = struct.unpack_from('<I', mtb_data, i)[0]
        if 0 < val <= 50 and i + 4 < min(len(mtb_data), matp_off + 20 + u2):
            next_val = struct.unpack_from('<I', mtb_data, i + 4)[0]
            if next_val == 0 or next_val == 0xFFFFFFFF:
                if i - last_ref_pos > 100 and current_cluster:
                    prop_tex_lists.append(current_cluster); current_cluster = []
                current_cluster.append(val)
                last_ref_pos = i
                if next_val == 0xFFFFFFFF: prop_tex_lists.append(current_cluster); current_cluster = []
                i += 8; continue
        elif val == 0xFFFFFFFF and current_cluster:
            prop_tex_lists.append(current_cluster); current_cluster = []; last_ref_pos = i
        i += 4
    if current_cluster: prop_tex_lists.append(current_cluster)
    return {mat_i: prop_tex_lists[prop_i][0] for mat_i, prop_i in enumerate(mat_to_prop) if prop_i < len(prop_tex_lists) and prop_tex_lists[prop_i]}
