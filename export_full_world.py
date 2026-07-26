#!/usr/bin/env python3
"""Export full world geometry + placed env_assets + track surface to glTF."""
import sys, os, struct, json, base64, zipfile, math, re
sys.path.insert(0, 'RevOctane')
sys.path.insert(0, '.')
import bstream
from revoctane import Octane
from PIL import Image
from difflib import SequenceMatcher

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

def get_geometry_matrix(stnp, node_key):
    node = stnp.get(str(node_key))
    if node is None: return identity()
    return node.get('LocalToParentMatrix', identity())

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

def decode_texture(raw_data, width, height, fmt):
    import struct as s
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
                c0 = s.unpack_from('<H', block, 8)[0]; c1 = s.unpack_from('<H', block, 10)[0]
                c_bits = s.unpack_from('<I', block, 12)[0]
                r0 = ((c0>>11)&0x1F)*255//31; g0 = ((c0>>5)&0x3F)*255//63; b0 = (c0&0x1F)*255//31
                r1 = ((c1>>11)&0x1F)*255//31; g1 = ((c1>>5)&0x3F)*255//63; b1 = (c1&0x1F)*255//31
                alphas = [a0,a1,(6*a0+1*a1)//7,(5*a0+2*a1)//7,(4*a0+3*a1)//7,(3*a0+4*a1)//7,(2*a0+5*a1)//7,(1*a0+6*a1)//7] if a0>a1 else [a0,a1,(4*a0+1*a1)//5,(3*a0+2*a1)//5,(2*a0+3*a1)//5,(1*a0+4*a1)//5,0,255]
                colors = [(r0,g0,b0),(r1,g1,b1),((2*r0+r1)//3,(2*g0+g1)//3,(2*b0+b1)//3),((r0+2*r1)//3,(g0+2*g1)//3,(b0+2*b1)//3)] if c0>c1 else [(r0,g0,b0),(r1,g1,b1),((r0+r1)//2,(g0+g1)//2,(b0+b1)//2),(0,0,0)]
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
                c0=s.unpack_from('<H',block,0)[0]; c1=s.unpack_from('<H',block,2)[0]
                bits=s.unpack_from('<I',block,4)[0]
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
    elif fmt == 'RGBA8': return raw_data[:width*height*4]
    else:
        for py in range(height):
            for px in range(width):
                pi = (py*width+px)*4; pixels[pi:pi+4] = [255,0,255,255]
    return bytes(pixels)

# (Removed gen_track_surface)

# ── Env asset index & resolution ──

def index_env_assets(env_root):
    assets = {}
    for root, dirs, files in os.walk(env_root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.endswith('.zip') and not f.startswith('._'):
                path = os.path.join(root, f)
                base = f.replace('.zip', '').lower()
                assets[base] = path
    print(f'  Indexed {len(assets)} env_asset ZIPs')
    return assets

def normalize_name(name):
    n = name.lower()
    n = re.sub(r'^placement_', '', n)
    n = re.sub(r'_\d+$', '', n)
    n = re.sub(r'(?<=[a-z])(?=[A-Z])', '_', n)
    return n

def find_best_asset(placement_name, env_index):
    name = placement_name.lower()
    name = re.sub(r'^placement_', '', name)
    pn = re.sub(r'_\d+$', '', name)
    pn = re.sub(r'^cars3_', '', pn)
    pn = re.sub(r'^thw_', '', pn)
    best_ratio = 0.55
    best_path = None
    for key, path in env_index.items():
        kn = key
        kn = re.sub(r'^cars3_', '', kn)
        kn = re.sub(r'^thw_', '', kn)
        ratio = SequenceMatcher(None, pn, kn).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_path = path
        if pn in kn or kn in pn:
            score = min(len(pn), len(kn))
            if score > best_ratio * 10:
                best_ratio = score / 10
                best_path = path
    return best_path, best_ratio

def extract_asset_geometry(zip_path, mat_offset=0):
    """Extract geometry primitives from an env_asset ZIP (no world_mat applied)."""
    zip_data = load_zip(zip_path)
    oct_name = next((n for n in zip_data if n.endswith('.oct')), None)
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
        world_mat = get_geometry_matrix(stnp, k)
        prims = v.get('Primitives', {})
        for pk in sorted(prims.keys(), key=lambda x: int(x)):
            prim = prims[pk]
            if not isinstance(prim, dict): continue
            mat_name = prim.get('MaterialName', 'unknown')
            if 'shadowcaster' in mat_name.lower(): continue
            vdata = [int(x) for x in prim.get('Vdata', [2,0,0,0,12,0,0,8])]
            idata = [int(x) for x in prim.get('Idata', [0,0,0,0])]
            vert_count, offset_a, stride_a = vdata[1], vdata[3], vdata[4]
            if offset_a + vert_count * stride_a > len(vbuf): continue
            if idata[1] + idata[3] * 2 > len(ibuf): continue
            prim_data = {
                'vdata': vdata, 'idata': idata,
                'unitBase': [float(x) for x in prim.get('UnitBase',[0,0,0])],
                'unitScale': [float(x) for x in prim.get('UnitScale',[1,1,1])],
                'ibuf': ibuf, 'indexWidth': 2,
            }
            pos, uvs, indices = decode_vbuf(vbuf, prim_data, world_mat)
            if len(pos) < 9 or len(indices) < 3: continue
            mat_ref = int(prim.get('MaterialReference', -1)) + mat_offset
            meshes.append({
                'positions': pos, 'uvs': uvs, 'indices': indices,
                'materialRef': mat_ref,
                'matName': mat_names.get(mat_ref - mat_offset, mat_name),
            })
    # Return material pool for remapping
    local_mat_pool = {}
    for k in sorted(mp.keys(), key=lambda x: int(x)):
        mname = mp[k].get('Name', mp[k].get('FileName', ''))
        local_mat_pool[int(k)] = mname
    return meshes, local_mat_pool

# ── Material registration ──

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

# ── World extraction ──

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
        oct_name = next((n for n in zip_data if n.endswith('.oct')), None)
        if not oct_name: continue
        stream = bstream.BStream(bytes=zip_data[oct_name])
        obj = Octane(stream)
        stnp = obj.get('SceneTreeNodePool', {})
        for k, v in stnp.items():
            if v.get('Type') == 'Geometry':
                node_name = v.get('NodeName', '')
                if 'polySurface1' in node_name:
                    world_mat = get_geometry_matrix(stnp, k)
                    if world_mat[0] == 1 and world_mat[5] == 1 and world_mat[10] == 1:
                        global_track_mat = world_mat
                        break
        if global_track_mat: break
    if global_track_mat:
        print(f'  Global track matrix: pos=({global_track_mat[12]:.1f},{global_track_mat[13]:.1f},{global_track_mat[14]:.1f})')

    # 1) Process static geometry and collect placements from world ZIPs
    placements = []
    master_render_mat = None
    for zn in zip_paths:
        path = os.path.join(romfs_dir, zn)
        if not os.path.isfile(path): continue
        zip_data = load_zip(path)
        oct_name = next((n for n in zip_data if n.endswith('.oct')), None)
        if not oct_name: continue
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

        geo_count = 0
        world_meshes = []
        for k, v in stnp.items():
            if v.get('Type') == 'Geometry':
                node_name = v.get('NodeName', k)
                skip_kw = ['lod1','lod2','lod3','_lod','shadow','proxy','collision','_dmg','damage','chassis_b','chassis_lod']
                if any(kw in node_name.lower() for kw in skip_kw): continue
                world_mat = get_geometry_matrix(stnp, k)
                prims = v.get('Primitives', {})
                for pk in sorted(prims.keys(), key=lambda x: int(x)):
                    prim = prims[pk]
                    if not isinstance(prim, dict): continue
                    mat_name = prim.get('MaterialName', 'unknown')
                    if 'shadowcaster' in mat_name.lower(): continue
                    vdata = [int(x) for x in prim.get('Vdata', [2,0,0,0,12,0,0,8])]
                    idata = [int(x) for x in prim.get('Idata', [0,0,0,0])]
                    vert_count, offset_a, stride_a = vdata[1], vdata[3], vdata[4]
                    if offset_a + vert_count * stride_a > len(vbuf): continue
                    if idata[1] + idata[3] * 2 > len(ibuf): continue
                    prim_data = {
                        'vdata': vdata, 'idata': idata,
                        'unitBase': [float(x) for x in prim.get('UnitBase',[0,0,0])],
                        'unitScale': [float(x) for x in prim.get('UnitScale',[1,1,1])],
                        'ibuf': ibuf, 'indexWidth': 2,
                    }
                    pos, uvs, indices = decode_vbuf(vbuf, prim_data, world_mat)
                    if len(pos) < 9 or len(indices) < 3: continue
                    mat_ref = int(prim.get('MaterialReference', -1))
                    world_meshes.append({
                        'positions': pos, 'uvs': uvs, 'indices': indices,
                        'materialRef': mat_ref, 'name': f'{node_name}_{pk}',
                        'matName': mat_names.get(mat_ref, mat_name),
                        'category': 'world',
                    })
                    geo_count += 1

            elif v.get('Type') == 'Transform':
                nn = v.get('NodeName', '')
                if nn and nn != 'Layout' and not nn.startswith('TRIG_'):
                    mat = v.get('LocalToParentMatrix', None)
                    placements.append((nn, mat))
                    if 'MasterRender' in nn:
                        master_render_mat = mat

        # Load MTB textures and material-texture mapping
        local_mtm = {}
        mtb_name = next((n for n in zip_data if n.lower().endswith('.mtb')), None)
        if mtb_name:
            import world_loader
            for t in world_loader.parse_mtb_textures(zip_data, zip_data[mtb_name]):
                if t['hash'] not in seen_tex:
                    seen_tex.add(t['hash']); all_tex.append(t)
            local_mtm = world_loader.parse_matp(zip_data[mtb_name]) or {}
        # Register materials with local MTM, get remapped MTM
        local_pool = {}
        for mk in sorted(mp.keys(), key=lambda x: int(x)):
            mname = mp[mk].get('Name', mp[mk].get('FileName', ''))
            local_pool[int(mk)] = mname
        mat_map, remapped_mtm = register_material_pool(local_pool, all_materials, local_mtm)
        mat_tex_map.update(remapped_mtm)
        for gm in world_meshes:
            gm['materialRef'] = mat_map.get(gm['materialRef'], -1)
        all_meshes.extend(world_meshes)

        # Generate track surface using global track matrix (only for the figure-8 variant)
        tnp = obj.get('TrackNodePool', {})
        tlp = obj.get('TrackLinkPool', {})
        if tnp and tlp and track_mat_idx is None:
            use_mat = global_track_mat if global_track_mat else identity()
            print(f'  Track surface ({len(tnp)} nodes, {len(tlp)} links)')
            tpos, tuvs, tidx = gen_track_surface(tnp, tlp, use_mat)
            if tpos:
                all_meshes.append({
                    'positions': tpos, 'uvs': tuvs, 'indices': tidx,
                    'materialRef': -1, 'name': '_track_surface',
                    'matName': 'TrackSurface',
                    'category': 'track',
                })
                geo_count += 1
                track_mat_idx = len(all_meshes) - 1

        print(f'  {zn}: {geo_count} geometry primitives')

    print(f'\n  {len(placements)} placement nodes found')

    # 2) Resolve placements to env_assets and load geometry
    asset_cache = {}
    placed_count = 0
    resolved = 0
    for pname, pmat in placements:
        if pmat is None: continue
        path, ratio = find_best_asset(pname, env_index)
        if not path:
            continue
        resolved += 1
        if path not in asset_cache:
            geoms, local_pool = extract_asset_geometry(path)
            mat_map, _ = register_material_pool(local_pool, all_materials, None)
            for gm in geoms:
                gm['materialRef'] = mat_map.get(gm['materialRef'], -1)
            asset_cache[path] = geoms
        geoms = asset_cache[path]
        for mesh in geoms:
            pos = mesh['positions']
            uvs = mesh['uvs']
            # Apply placement transform to each vertex
            new_pos = []
            for i in range(0, len(pos), 3):
                tx, ty, tz = transform_point(pmat, (pos[i], pos[i+1], pos[i+2]))
                new_pos.extend([tx, ty, tz])
            all_meshes.append({
                'positions': new_pos, 'uvs': uvs, 'indices': mesh['indices'],
                'materialRef': mesh['materialRef'],
                'name': f'{pname}',
                'matName': mesh['matName'],
                'category': 'props',
            })
            placed_count += 1

    print(f'  Resolved {resolved}/{len(placements)} placements, placed {placed_count} primitives')
    print(f'  Unique env_assets loaded: {len(asset_cache)}')

    # 3) Load well-known env_assets (8track figure-8 track model + countryside terrain)
    wk_root = os.path.join(env_assets_dir, 'env_assets', 'cars3')
    well_known = [
        ('cars3_thw/cars3_thw_8track/cars3_thw_8track.zip', '8track'),
        ('cars3_thw/cars3_thw_countryside/cars3_thw_countryside.zip', 'countryside'),
    ]
    wk_count = 0
    for rel_path, label in well_known:
        apath = os.path.join(wk_root, rel_path)
        if not os.path.isfile(apath):
            continue
        wk_zip = load_zip(apath)
        geoms, local_pool = extract_asset_geometry(apath)
        wk_mtb = next((n for n in wk_zip if n.lower().endswith('.mtb')), None)
        local_mtm = {}
        if wk_mtb:
            import world_loader
            for t in world_loader.parse_mtb_textures(wk_zip, wk_zip[wk_mtb]):
                if t['hash'] not in seen_tex:
                    seen_tex.add(t['hash']); all_tex.append(t)
            local_mtm = world_loader.parse_matp(wk_zip[wk_mtb]) or {}
        mat_map, remapped_mtm = register_material_pool(local_pool, all_materials, local_mtm)
        mat_tex_map.update(remapped_mtm)
        for gm in geoms:
            gm['materialRef'] = mat_map.get(gm['materialRef'], -1)
        if not geoms:
            continue
        wk_mat = master_render_mat if master_render_mat else identity()
        for mesh in geoms:
            pos = mesh['positions']
            new_pos = []
            for i in range(0, len(pos), 3):
                tx, ty, tz = transform_point(wk_mat, (pos[i], pos[i+1], pos[i+2]))
                new_pos.extend([tx, ty, tz])
            all_meshes.append({
                'positions': new_pos, 'uvs': mesh['uvs'],
                'indices': mesh['indices'],
                'materialRef': mesh['materialRef'],
                'name': label,
                'matName': mesh['matName'],
                'category': label,
            })
            wk_count += 1
    print(f'  Well-known assets loaded: {wk_count} primitives')

    return all_meshes, all_tex, all_materials, mat_tex_map, track_mat_idx, len(asset_cache)


def export_gltf(meshes, tex_list, materials, mat_tex_map, out_name, track_mesh_idx, num_assets, tex_pngs=None):
    if not meshes: print('No meshes'); return False
    if tex_pngs is None:
        tex_pngs = {}
        for i, t in enumerate(tex_list):
            raw = base64.b64decode(t['data'])
            rgba = decode_texture(raw, t['width'], t['height'], t['format'])
            Image.frombytes('RGBA', (t['width'], t['height']), rgba).save(f'{out_name}_tex_{i}.png')
            tex_pngs[t['mtbIndex']] = f'{out_name}_tex_{i}.png'

    gltf = {
        'asset': {'version': '2.0', 'generator': 'cars3_viewer'},
        'scene': 0, 'scenes': [{'nodes': [0]}],
        'nodes': [{'mesh': 0, 'name': out_name}],
        'meshes': [], 'accessors': [], 'bufferViews': [], 'buffers': [],
        'samplers': [{'magFilter': 9729, 'minFilter': 9987, 'wrapS': 10497, 'wrapT': 10497}],
        'textures': [], 'images': [], 'materials': [],
    }
    bin_data = bytearray()
    mesh_dict = {'primitives': [], 'name': out_name}
    mat_gltf_map = {}

    for mi, m in enumerate(materials):
        idx = m.get('index', mi)
        gltf_mat = {
            'name': m.get('name', f'Mat_{mi}'),
            'pbrMetallicRoughness': {'baseColorFactor': [0.8,0.8,0.8,1.0], 'metallicFactor': 0.0, 'roughnessFactor': 0.8},
        }
        tex_ref = mat_tex_map.get(str(idx), mat_tex_map.get(idx))
        if tex_ref is not None and tex_ref in tex_pngs:
            gltf['images'].append({'uri': tex_pngs[tex_ref]})
            gltf['textures'].append({'sampler': 0, 'source': len(gltf['images'])-1})
            gltf_mat['pbrMetallicRoughness']['baseColorTexture'] = {'index': len(gltf['textures'])-1, 'texCoord': 0}
        gltf['materials'].append(gltf_mat)
        mat_gltf_map[idx] = mi

    track_mat_idx_gltf = len(gltf['materials'])
    gltf['materials'].append({
        'name': 'TrackSurface',
        'pbrMetallicRoughness': {'baseColorFactor': [0.6,0.5,0.3,1.0], 'metallicFactor': 0.0, 'roughnessFactor': 0.9},
    })
    default_mat_idx = len(gltf['materials'])
    gltf['materials'].append({
        'name': 'Default',
        'pbrMetallicRoughness': {'baseColorFactor': [0.5,0.5,0.5,1.0], 'metallicFactor': 0.0, 'roughnessFactor': 0.8},
    })

    for mi, mesh in enumerate(meshes):
        pos = mesh['positions']; uvs = mesh['uvs']; indices = mesh['indices']
        mref = mesh.get('materialRef', 0)
        if len(pos) < 9: continue
        pos_bytes = struct.pack(f'<{len(pos)}f', *pos)
        pos_off = len(bin_data); bin_data.extend(pos_bytes)
        has_uv = len(uvs) > 0 and len(uvs[0]) == 2
        if has_uv:
            uv_bytes = b''.join(struct.pack('<ff', u, v) for u, v in uvs)
        else:
            uv_bytes = b''
        uv_off = len(bin_data); bin_data.extend(uv_bytes)
        idx_bytes = struct.pack(f'<{len(indices)}H', *indices)
        idx_off = len(bin_data); bin_data.extend(idx_bytes)

        def bv(o, l):
            i = len(gltf['bufferViews'])
            gltf['bufferViews'].append({'buffer': 0, 'byteOffset': o, 'byteLength': l})
            return i

        vc = len(pos)//3
        pos_acc = len(gltf['accessors'])
        gltf['accessors'].append({
            'bufferView': bv(pos_off, len(pos_bytes)), 'componentType': 5126,
            'count': vc, 'type': 'VEC3',
            'min': [min(pos[i*3] for i in range(vc)), min(pos[i*3+1] for i in range(vc)), min(pos[i*3+2] for i in range(vc))],
            'max': [max(pos[i*3] for i in range(vc)), max(pos[i*3+1] for i in range(vc)), max(pos[i*3+2] for i in range(vc))],
        })
        if has_uv:
            uv_acc = len(gltf['accessors'])
            gltf['accessors'].append({
                'bufferView': bv(uv_off, len(uv_bytes)), 'componentType': 5126,
                'count': len(uvs), 'type': 'VEC2',
            })
        idx_acc = len(gltf['accessors'])
        gltf['accessors'].append({
            'bufferView': bv(idx_off, len(idx_bytes)), 'componentType': 5123,
            'count': len(indices), 'type': 'SCALAR',
            'min': [min(indices)], 'max': [max(indices)],
        })
        if mi == track_mesh_idx:
            mat_ref = track_mat_idx_gltf
        else:
            mat_ref = mat_gltf_map.get(mref, default_mat_idx)
        attrs = {'POSITION': pos_acc}
        if has_uv: attrs['TEXCOORD_0'] = uv_acc
        mesh_dict['primitives'].append({
            'attributes': attrs,
            'indices': idx_acc, 'material': mat_ref,
        })

    gltf['meshes'].append(mesh_dict)
    bin_data = bytes(bin_data)
    gltf['buffers'].append({'byteLength': len(bin_data), 'uri': f'{out_name}.bin'})
    with open(f'{out_name}.gltf', 'w') as f: json.dump(gltf, f, indent=2)
    with open(f'{out_name}.bin', 'wb') as f: f.write(bin_data)
    xs = []; ys = []; zs = []
    for m in meshes:
        for i in range(0, len(m['positions']), 3):
            xs.append(m['positions'][i]); ys.append(m['positions'][i+1]); zs.append(m['positions'][i+2])
    print(f'Exported {out_name}.gltf + {out_name}.bin')
    print(f'  Primitives: {len(meshes)}, Textures: {len(tex_pngs)}, Buffer: {len(bin_data)} bytes, EnvAssets: {num_assets}')
    if xs: print(f'  Bounds: X[{min(xs):.1f},{max(xs):.1f}] Y[{min(ys):.1f},{max(ys):.1f}] Z[{min(zs):.1f},{max(zs):.1f}]')
    return True


if __name__ == '__main__':
    romfs_dir = 'romfs/assets/worlds'
    env_root = 'romfs/assets'
    out_name = 'thunder_hollow_full'

    zips = sorted(f for f in os.listdir(romfs_dir) if 'thunderhollow' in f and f.endswith('.zip') and not f.startswith('._'))
    if not zips: print(f'No thunderhollow ZIPs in {romfs_dir}'); sys.exit(1)

    print('Indexing env_assets...')
    env_index = index_env_assets(env_root)

    print(f'Processing {len(zips)} world ZIPs:')
    meshes, tex_list, materials, mat_tex_map, track_mesh_idx, num_assets = extract_world_full(zips, romfs_dir, env_root, env_index)

    print(f'\nTotal: {len(meshes)} primitives, {len(tex_list)} textures, {len(materials)} materials, {num_assets} env_assets')
    if not meshes: sys.exit(0)

    # Decode textures once, share across all categories
    shared_tex_pngs = {}
    for i, t in enumerate(tex_list):
        raw = base64.b64decode(t['data'])
        rgba = decode_texture(raw, t['width'], t['height'], t['format'])
        Image.frombytes('RGBA', (t['width'], t['height']), rgba).save(f'{out_name}_tex_{i}.png')
        shared_tex_pngs[t['mtbIndex']] = f'{out_name}_tex_{i}.png'

    # Partition meshes by category and export separately
    categories = {}
    for m in meshes:
        cat = m.get('category', 'other')
        categories.setdefault(cat, []).append(m)

    for cat, cat_meshes in sorted(categories.items()):
        cat_out = f'{out_name}_{cat}'
        # Find track_mesh_idx within this category
        cat_track_idx = None
        if track_mesh_idx is not None:
            count_before = 0
            for i, m in enumerate(meshes):
                if i == track_mesh_idx:
                    if m.get('category') == cat:
                        cat_track_idx = count_before
                    break
                if m.get('category') == cat:
                    count_before += 1
        cat_assets = num_assets if cat in ('props',) else 0
        export_gltf(cat_meshes, tex_list, materials, mat_tex_map, cat_out, cat_track_idx, cat_assets, shared_tex_pngs)
        print(f'  Exported {cat_out}.gltf ({len(cat_meshes)} primitives)')
