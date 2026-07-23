#!/usr/bin/env python3
"""
Cars 3 romfs explorer — model viewer + full romfs browser.
Serves OCT/VBUF/IBUF geometry, textures, and any file from romfs.
"""
import os, sys, json, base64, struct, re, traceback, zipfile, mimetypes, hashlib
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote

REVDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'RevOctane')
sys.path.insert(0, REVDIR)
import bstream
from revoctane import Octane

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROMFS_DIR = os.path.join(BASE_DIR, 'romfs')
CHARACTERS_DIR = os.path.join(ROMFS_DIR, 'assets', 'characters')
PORT = 8766

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
                    try:
                        with zipfile.ZipFile(fpath, 'r') as z:
                            for n in z.namelist():
                                if n.lower().endswith('.oct'):
                                    has_model = True
                                    break
                    except Exception:
                        pass
                    if has_model:
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
    for entry in sorted(os.listdir(BUNDLES_DIR)):
        if entry.startswith('.') or not entry.endswith('.zip'):
            continue
        name = entry.replace('.zip', '')
        cat_key = name.split('_')[0] if '_' in name else 'other'
        cat_name = BUNDLE_CATEGORIES.get(cat_key, cat_key.title())
        if cat_name not in categories:
            categories[cat_name] = []
        bundle_path = os.path.join(BUNDLES_DIR, entry)
        has_model = False
        try:
            with zipfile.ZipFile(bundle_path, 'r') as z:
                for n in z.namelist():
                    if n.lower().endswith('.oct'):
                        has_model = True
                        break
        except Exception:
            pass
        categories[cat_name].append({
            'id': name,
            'name': name,
            'file': entry,
            'hasModel': has_model,
        })
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

def find_oct_with_vbuf(zip_data):
    oct_entries = [(n, r) for n, r in zip_data.items() if n.lower().endswith('.oct')]
    if not oct_entries:
        return None, None
    oct_entries.sort(key=lambda x: (0 if '/' not in x[0] else 1, -len(x[1])))
    for name, raw in oct_entries:
        try:
            stream = bstream.BStream(bytes=raw)
            obj = Octane(stream)
            vpool = obj.get('VertexBufferPool', {})
            if vpool:
                return obj, raw
        except Exception:
            continue
    return None, None

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

    prims = []
    geom_nodes = find_geometry_nodes(obj)
    for node_key, node in geom_nodes:
        node_name = node.get('NodeName', node_key)
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

# ── MTB texture parsing ──

COMMON_WIDTHS = [128, 256, 320, 384, 512, 64, 1024, 640, 768]

TEXTURE_FORMATS = {
    0x5E: 'BC3',
    0x9E: 'BC3',
    0x5C: 'BC1',
    0x9C: 'BC1',
    0x9B: 'BC7',
    0x5A: 'RGBA8',
    0x98: 'BC3',
    0x70: 'BC5',
    0xB0: 'BC5',
    0x60: 'BC4',
    0xA0: 'BC4',
}

def parse_mtb_textures(zip_data, mtb_data):
    """Parse MTB file to extract texture info including format and slot."""
    textures = []
    if len(mtb_data) < 0x28:
        return textures
    num_tex = struct.unpack_from('<I', mtb_data, 0x1C)[0]
    for i in range(num_tex):
        base = 0x28 + i * 16
        if base + 16 > len(mtb_data):
            break
        hash_hex = mtb_data[base:base+8].hex()
        v1 = struct.unpack_from('<H', mtb_data, base+10)[0]
        slot = mtb_data[base+14]

        tbody_name = None
        for name in zip_data:
            if name.endswith(f'{hash_hex}.tbody'):
                tbody_name = name
                break
        if not tbody_name:
            continue
        data = zip_data[tbody_name]
        if len(data) < 16:
            continue

        size = len(data)
        height = struct.unpack_from('<H', mtb_data, base+13)[0]
        if height < 4:
            height = v1
        if height % 4 != 0:
            height = (height // 4 + 1) * 4

        block_size = 16
        blocks = size // block_size
        width = 0
        if height > 0 and blocks % (height // 4) == 0:
            width = (blocks // (height // 4)) * 4
        if width == 0 or width > 4096 or height > 4096:
            for try_w in COMMON_WIDTHS:
                bw = try_w // 4
                if bw > 0 and blocks % bw == 0:
                    bh = blocks // bw
                    th = bh * 4
                    if 4 <= th <= 8192:
                        width = try_w
                        height = th
                        break
        if width == 0:
            continue

        fmt_code = TEXTURE_FORMATS.get(v1, 'BC3')

        textures.append({
            'hash': hash_hex,
            'width': width,
            'height': height,
            'size': size,
            'format': fmt_code,
            'slot': slot,
            'mtbIndex': i,
            'data': base64.b64encode(data).decode(),
        })
    return textures


def parse_matp(mtb_data):
    """Parse MATP section to extract material-to-texture mapping.

    Returns dict: {material_index: texture_index} for diffuse textures.
    """
    matp_off = mtb_data.find(b'MATP')
    if matp_off < 0:
        return {}

    off = matp_off + 4
    u1 = struct.unpack_from('<I', mtb_data, off)[0]; off += 4
    u2 = struct.unpack_from('<I', mtb_data, off)[0]; off += 4
    num_mat = struct.unpack_from('<I', mtb_data, off)[0]; off += 4
    num_prop = struct.unpack_from('<I', mtb_data, off)[0]; off += 4

    if num_mat == 0 or num_prop == 0:
        return {}

    off += num_prop * 32  # skip property hashes

    # Skip pre-UUID header (find first UUID)
    uuid_re = re.compile(rb'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    m = uuid_re.search(mtb_data, off)
    if not m:
        return {}
    uuid_start = m.start()

    # Read UUIDs (36 chars + null separator each)
    off = uuid_start
    for _ in range(num_mat):
        off += 37  # 36 UUID chars + 1 null

    # Scan for the uint16 property index array (all values < num_prop)
    mat_to_prop = None
    for try_off in range(off - 2, min(off + 16, len(mtb_data) - num_mat * 2)):
        if try_off % 2 != 0:
            continue
        vals = []
        ok = True
        for j in range(num_mat):
            v = struct.unpack_from('<H', mtb_data, try_off + j * 2)[0]
            if v >= num_prop and v != 0:
                ok = False
                break
            vals.append(v)
        if ok and len(vals) == num_mat and sum(1 for v in vals if v > 0) >= num_mat - 2:
            mat_to_prop = vals
            off = try_off + num_mat * 2
            break

    if mat_to_prop is None:
        return {}

    # Align to 4 bytes
    off = (off + 3) & ~3

    # Now scan the property block data for texture reference lists.
    # Pattern: uint32 tex_idx (1-21), uint32(0), ..., uint32(0xFFFFFFFF) sentinel
    # Clusters of these are separated by >100 bytes of shader param data.
    prop_tex_lists = []
    block_data_end = min(len(mtb_data), matp_off + 20 + u2)

    i = off
    current_cluster = []
    last_ref_pos = -200

    while i + 8 <= block_data_end:
        val = struct.unpack_from('<I', mtb_data, i)[0]
        if 0 < val <= 50 and i + 4 < block_data_end:
            next_val = struct.unpack_from('<I', mtb_data, i + 4)[0]
            if next_val == 0:
                # Found a tex_idx, 0 pair - check if this is part of a cluster
                if i - last_ref_pos > 100 and current_cluster:
                    prop_tex_lists.append(current_cluster)
                    current_cluster = []
                current_cluster.append(val)
                last_ref_pos = i
                i += 8
                continue
            elif next_val == 0xFFFFFFFF:
                # tex_idx followed by sentinel
                if i - last_ref_pos > 100 and current_cluster:
                    prop_tex_lists.append(current_cluster)
                    current_cluster = []
                current_cluster.append(val)
                prop_tex_lists.append(current_cluster)
                current_cluster = []
                last_ref_pos = i
                i += 8
                continue
        elif val == 0xFFFFFFFF and current_cluster:
            prop_tex_lists.append(current_cluster)
            current_cluster = []
            last_ref_pos = i
        i += 4

    if current_cluster:
        prop_tex_lists.append(current_cluster)

    # Match property blocks to texture lists (by order)
    # Build material -> diffuse texture index mapping
    mat_tex_map = {}
    for mat_i in range(num_mat):
        prop_i = mat_to_prop[mat_i]
        if prop_i < len(prop_tex_lists):
            tex_indices = prop_tex_lists[prop_i]
            if tex_indices:
                mat_tex_map[mat_i] = tex_indices[0]  # first texture in block

    return mat_tex_map

def extract_character_data(char_id):
    char_dir = os.path.join(CHARACTERS_DIR, char_id)
    if not os.path.isdir(char_dir):
        return None, f'Character directory not found: {char_id}'
    zip_files = [f for f in os.listdir(char_dir) if f.endswith('.zip') and not f.startswith('._')]
    if not zip_files:
        return None, 'No zip files found'

    # Sort: prefer ZIP matching char_id name (the base model), skip color variants
    primary = char_id + '.zip'
    if primary in zip_files:
        zip_files = [primary]
    else:
        zip_files = [zip_files[0]]

    groups = []
    tex_list = []
    mat_list = []
    mat_tex_map = {}
    seen_vbuf_hashes = set()

    for zf_name in zip_files:
        try:
            zip_data = load_zip(os.path.join(char_dir, zf_name))
        except Exception:
            continue
        try:
            obj, raw = find_oct_with_vbuf(zip_data)
        except Exception:
            continue
        if not obj:
            continue

        vpool = obj.get('VertexBufferPool', {})
        ibuf_pool = obj.get('IndexBufferPool', {})
        vbuf_fn = ibuf_fn = None
        for k in vpool:
            fn = vpool[k].get('FileName', '')
            if fn and fn in zip_data:
                vbuf_fn = fn
                break
        for k in ibuf_pool:
            fn = ibuf_pool[k].get('FileName', '')
            if fn and fn in zip_data:
                ibuf_fn = fn
                break

        # Deduplicate by hashing actual vbuf bytes
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
            mat_list.append({
                'index': int(k),
                'name': m.get('Name', ''),
                'fileName': m.get('FileName', ''),
            })

        for name, data in zip_data.items():
            if name.endswith('.mtb'):
                tex_list.extend(parse_mtb_textures(zip_data, data))
                matp = parse_matp(data)
                if matp:
                    mat_tex_map.update(matp)

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

    groups = []
    tex_list = []
    mat_list = []
    mat_tex_map = {}
    seen_vbuf_keys = set()

    try:
        obj, raw = find_oct_with_vbuf(zip_data)
    except Exception:
        obj = None

    if obj:
        vpool = obj.get('VertexBufferPool', {})
        ibuf_pool = obj.get('IndexBufferPool', {})
        vbuf_fn = ibuf_fn = None
        for k in vpool:
            fn = vpool[k].get('FileName', '')
            if fn and fn in zip_data:
                vbuf_fn = fn
                break
        for k in ibuf_pool:
            fn = ibuf_pool[k].get('FileName', '')
            if fn and fn in zip_data:
                ibuf_fn = fn
                break
        dedup_key = (vbuf_fn, ibuf_fn)

        group = extract_buffer_group(zip_data, obj)
        if group:
            groups.append(group)

        matpool = obj.get('MaterialPool', {})
        for k in sorted(matpool.keys(), key=lambda x: int(x)):
            m = matpool[k]
            mat_list.append({
                'index': int(k),
                'name': m.get('Name', ''),
                'fileName': m.get('FileName', ''),
            })

        for name, data in zip_data.items():
            if name.endswith('.mtb'):
                tex_list.extend(parse_mtb_textures(zip_data, data))
                matp = parse_matp(data)
                if matp:
                    mat_tex_map.update(matp)

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

# ── HTTP handler ──

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = parse_qs(parsed.query)

        try:
            if path == '' or path == '/' or path == '/index.html':
                self.send_file(os.path.join(BASE_DIR, 'viewer.html'), 'text/html')

            elif path.endswith('.js') or path.endswith('.css') or path.endswith('.png') or path.endswith('.ico'):
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
                    self.send_json({
                        'type': 'zipfile',
                        'containerPath': rel,
                        'name': os.path.basename(inner),
                        'size': len(raw),
                        'ext': ext,
                        'data': b64,
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

            else:
                self.send_error(404)

        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({'error': str(e)}, 500)
            except Exception:
                pass

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
