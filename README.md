![Banner](banner.png)

<div align="center">

  <img src="logo.png" alt="Logo" width="44" height="44">

  # Cars 3 Driven to Win - Explorer & Mod Tool

  A web-based 3D model viewer, file explorer, and mod maker for *Cars 3: Driven to Win* (Nintendo Switch).

</div>

---

### Features

- **3D Model Viewer** — Three.js r143 renders 70+ characters with full armature skeleton, bone visualization, wireframe/spin/grid toggles
- **glTF Import** — Drag-and-drop glTF/GLB files; texture loading with async canvas placeholder + onload callback; auto-scaling/centering
- **glTF Export with Animation** — Exports skinned meshes with bone hierarchy, inverse bind matrices, JOINTS_0/WEIGHTS_0, and animation clips (pose & keyframe tracks)
- **Animation Playback** — Load 268+ animation clips per character; pose (count=0) and multi-keyframe clips; mixer-based playback with play/stop/reset
- **Model Replacement (Mod Tool)** — "Save as Mod" converts an imported glTF mesh to game vbuf/ibuf format (int16 positions + float16 UVs), writes to `.mod_workspace/`, and reloads via `?workspace=1`
- **Texture Editing** — Click any texture to open canvas editor; paint or replace image; save back to workspace (BC1/BC3/RGBA8 encode)
- **Shared Texture Archives** — Loads `.tstream` textures from `romfs/assets/textures/` archives; tstream size formula: `data_size + 2 × next_power_of_2(data_size)`
- **Lua Disassembler** — Readable pseudo-code output for all 676 game scripts (non-standard Lua 5.1 bytecode: op(6)/A(8)/C(9)/B(9) layout)
- **Texture Viewer** — BC1/BC3/BC4/BC5/BC7 texture decode with format identification
- **File Browser** — Browse the entire romfs filesystem and ZIP archives
- **CSS Design** — Modern dark UI with accent theming, backdrop blur, and smooth transitions

## Running Locally

**Prerequisites**
- Python 3.10+ with `pillow` (`pip3 install pillow`)
- `luac51` for Lua compilation (optional, for mod script editing)

**Run**
```bash
cd /path/to/Cars3mtw
python3 cars3_viewer.py
```

Open `http://localhost:8766` in your browser.

**Game Data**

Place your Nintendo Switch romfs dump in `romfs/`. All game assets are inside ZIP archives.
```
romfs/
  bundles/          # Character and world bundles (actor-*.zip, zone-*.zip)
  assets/           # UI, weapons, tracks, etc.
  worlds/           # World data
```

---

## Directory Layout

```
Cars3mtw/
  cars3_viewer.py        # Backend server (Python stdlib http.server)
  viewer.html            # Frontend (Three.js model viewer + mod UI)
  world_loader.py        # OCT/VBuf/IBuf parser, texture decoder, MATP reader
  bc7_decoder.js         # Client-side BC7 texture decoder
  three.min.js           # Three.js r143
  logo.png               # App logo
  banner.png             # App banner
  romfs/                 # Game data (romfs dump as ZIPs)
  RevOctane/             # Read-only OCT/MTB/Lua parsers (reference)
  .mod_workspace/        # Mod workspace (auto-created)
  exefs/                 # Game executable data (for mod patching)
```

---

## Game File Formats

### OCT (Octane Scene Container)

Binary mesh/scene container. All characters and worlds use this format.

```
Header:
  4 bytes: Magic "OCT\x00" or similar
  ...scene hierarchy, transforms, material references

Contains:
  - Node tree (transforms, parent/child)
  - Mesh references pointing to VBuf + IBuf pairs
  - Material assignments per mesh primitive
  - Motion files in characters/<name>/motions/ subdirectories
```

Parsed by `world_loader.py:parse_oct()` and `RevOctane/octane_parser.py`.

### VBuf (Vertex Buffer)

Raw vertex data. Layout per vertex depends on stride.

```
Per-primitive header (from OCT):
  numStreams, vertCount, _pad, offsetA, strideA, _pad, offsetB, strideB

  stream A: positions (int16 x3) + UVs (float16 x2), stride 12
  stream B: skin indices (uint8 x4) + skin weights (uint8 x4), stride 8

  unitBase[3] (float32 x3) - position origin
  unitScale[3] (float32 x3) - position scale
```

Position decode: `world = unitBase + (int16_value / 32767.0) * unitScale`

Two-stream layout: stream A at `offset_a`/`stride_a` followed immediately by stream B at `offset_b=vertCount*stride_a`/`stride_b=8`.

### IBuf (Index Buffer)

Triangle index data. Supports 1-byte, 2-byte, and 4-byte indices.

```
Per-primitive header (from OCT):
  ibRef, byteOffset, baseIndex, indexCount
  indexWidth (1, 2, or 4 bytes)

Index decode: raw_value - baseIndex
```

### MTB (Material Binary)

Material definitions and texture references.

```
Header (LE):
  Offset 0x1C: numTextures (uint32)

Texture entries (16 bytes each, starting at 0x28):
  Offset 0:  8 bytes - texture hash (used as filename key)
  Offset 10: uint16 - raw format code
  Offset 13: b13 - texture height hint (may not be multiple of 4)
  Offset 14: slot - texture slot (0=diffuse, 1=lightmap, 2=normal, 5=detail)

MATP section (at "MATP" magic):
  Maps material index -> texture index
```

Format codes:

| Code | Format | Block Size |
|------|--------|------------|
| 0x5E, 0x9E, 0x98 | BC3 | 16 bytes |
| 0x5C, 0x9C | BC1 | 8 bytes |
| 0x9B | BC7 | 16 bytes |
| 0x5A, 0x98 | RGBA8 | raw |
| 0x70, 0xB0 | BC5 | 16 bytes |
| 0x60, 0xA0 | BC4 | 8 bytes |

### TBody (Texture Body)

Raw compressed texture data. No header - starts directly with block data. All multi-byte values are **little-endian**.

```
For BC3: Array of 16-byte blocks, laid out row-major in 4x4 pixel blocks.
  Block bytes 0-1:   alpha0, alpha1
  Block bytes 2-7:   48-bit alpha indices (3 bits per pixel, 16 pixels)
  Block bytes 8-9:   color0 (RGB565, little-endian)
  Block bytes 10-11: color1 (RGB565, little-endian)
  Block bytes 12-15: 32-bit color indices (2 bits per pixel, 16 pixels)

BC3 decode tables (per BCn spec):
  c0 > c1: colors = [c0, (2c0+c1)/3, (c0+2c1)/3, c1]
  c0 <= c1: colors = [c0, (c0+c1)/2, c1, (0,0,0)]

  a0 > a1: alphas = [a0, (6a0+1a1)/7, ..., (1a0+6a1)/7, a1]  (8 entries)
  a0 <= a1: alphas = [a0, (4a0+1a1)/5, ..., (1a0+4a1)/5, 0, 255, a1]
```

### TStream Format

Found in `romfs/assets/textures/` as `.tszip` or `.zip` archives. Each contains `.tstream` files with mip levels stored **smallest to largest** (opposite of typical order).

```
tstream_size = data_size + 2 * next_power_of_2(data_size)

The full-size (base) texture data is the last `data_size` bytes of the tstream.
The function tstream_data_size(tstream_file_size) extracts data_size from
the file size using: data_size = (tstream_size) / (1 + 2/next_power_of_2(...))
```

Character textures come from game bundles (`bundles/actor-{char}/_root_/textures/`), not the shared texture archives. The shared archives are for environment/world streaming textures.

### ZIP Packaging

All game assets are stored in ZIP archives. A single character's ZIP contains:
- Multiple `.tbody` files (one per texture mip level / slot)
- One `.mtb` file (material definitions)
- Multiple `.oct` files (mesh data)
- Corresponding `.vbuf` and `.ibuf` files

Character ZIPs are in `romfs/bundles/actor-cars3_*/_root_.zip`.
World ZIPs are in `romfs/bundles/zone-cars3_*/_root_.zip`.

---

## Animation System

### Clip Types

Two types from the server:
- **Pose clips** (`count=0`): Single-frame animation with a `pose` map of `boneName → {quaternion, position}`. Playback holds the pose over the clip duration.
- **Animated clips** (`count>0`): Multi-keyframe animation with per-bone `keyframes[]` containing `times[]`, `quaternion_values[]`, `position_values[]`.

### Data Flow

1. Server reads motion `.oct` files from character ZIPs (`characters/<name>/motions/<clip>.oct`)
2. `extract_animation_data()` parses the OCT ClipDataBlock (via `parse_clip_data_block()` in `world_loader.py`)
3. Frontend `playAnimationData()` builds THREE.KeyframeTrack objects, composes rest pose × animation delta via Quaternion multiplication
4. `lastAnimData` stores the full server response for use during glTF export

### Bone Name Matching

Pose bones are a subset of armature bones. For Arvy: 12 pose bones ⊂ 40 armature bones. All bone names match by string comparison.

### glTF Animation Export

Both `pose()` and `keyframe()` clips are exported:
- Creates per-bone `samplers` (input times + output rotation/translation)
- Composes: `finalQuat = restQuat * deltaQuat`, `finalPos = restPos + deltaPos`
- Uses `gltf.animations[0].channels[]` with `{sampler, target: {node, path}}`

### Known Issues

- **Command block keyframe format**: Multi-frame clips with `count>0` use a command-block format whose uint16 encoding is not yet fully decoded. The float pool values for such clips are base/default poses; actual keyframes are in the command blocks.
- **Quaternion alignment drift**: The 7-float grouping scheme works for early bones but drifts for later ones. The command template controls float→bone mapping but descriptor→bone index translation is unsolved.

---

## Lua 5.1 Bytecode (Game Scripts)

The game ships ~676 Lua scripts (660 `.lua`, 10 `.sx`), all compiled to Lua 5.1 bytecode.

### Header Format (12 bytes)

```
Offset 0-3:  Signature "\x1bLua" (0x1B 0x4C 0x75 0x61)
Offset 4:    Version (0x51 = Lua 5.1)
Offset 5:    Format (0x00 = official)
Offset 6:    Endianness (0x00 = big-endian in game files, 0x01 = little-endian)
Offset 7:    Size of int (4)
Offset 8:    Size of size_t (4)
Offset 9:    Size of Instruction (4)
Offset 10:   Size of lua_Number (4)
Offset 11:   Integral flag (0 = float)
```

**Game files use big-endian (byte 6 = 0x00)** for header fields and constants. Instructions use the same endianness.

### Instruction Encoding (Non-Standard PUC Lua 5.1)

The game uses a **non-standard** instruction layout. Compared to PUC Lua 5.1:
- **A** field is 8 bits (not 9): bits 6-13
- **C** and **B** fields are **swapped**: C is at bits 14-22, B at bits 23-31
- **Bx** spans bits 14-31 (18 bits, same size but different position)

```
Bit layout (32-bit instruction word):
  Bits  0-5:   op (6 bits)
  Bits  6-13:  A  (8 bits)     ← standard uses 9 bits at 6-14
  Bits 14-22:  C  (9 bits)     ← standard has B here
  Bits 23-31:  B  (9 bits)     ← standard has C here

For iABx instructions:
  Bits 14-31:  Bx (18 bits)
  sBx = Bx - 131071
```

### Status

- Disassembly fully working — readable pseudo-code output for all ~676 game scripts
- Decompilation not possible. Scripts can be replaced by writing new Lua source and compiling with `luac51`.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/characters` | GET | List all 73 characters |
| `/api/character?id=NAME` | GET | Get character mesh + textures + armature |
| `/api/character_animations?id=NAME` | GET | List animation clips for a character |
| `/api/animation?char=NAME&clip=NAME` | GET | Get full animation clip data |
| `/api/asset?path=PATH` | GET | Load any OCT asset by path |
| `/api/browse?path=PATH` | GET | Browse romfs directory |
| `/api/file?path=PATH` | GET | Get file content |
| `/api/zip?path=PATH` | GET | List ZIP contents |
| `/api/zipfile?path=P&file=F` | GET | Get file from inside ZIP |
| `/api/search?q=QUERY` | GET | Search romfs filenames |
| `/api/scripts` | GET | List all Lua scripts |
| `/api/model/replace` | POST | Replace character model (vbuf/ibuf encode) |
| `/api/model/replace-tex` | POST | Replace character texture |
| `/api/model/workspace-files` | GET | List workspace files for a character |
| `/api/mod/workspace` | GET | List mod workspace files |
| `/api/mod/file` | GET/POST | Read/write workspace file |
| `/api/mod/compile` | POST | Compile Lua to 5.1 bytecode |
| `/api/mod/encode-tex` | POST | Encode PNG to BC3 |
| `/api/mod/extract` | POST | Extract from ZIP to workspace |
| `/api/mod/repack` | POST | Repack workspace into ZIP |
| `/api/mod/delete` | POST | Delete workspace file |
| `/api/mod/export` | POST | Export Ryujinx mod |
| `/api/mod/disassemble` | POST | Disassemble Lua bytecode |
| `/api/mod/romfs-zip` | GET | List romfs ZIP files |
| `/api/mod/export-check` | GET | Check export status |

---

## Known Issues

- **Texture decode fixes applied:**
  - BCn lookup tables corrected — alpha/color endpoint indices were shifted (e.g. `a1`/`c1` at index 1 instead of last index). Fixed in both Python `decode_texture_raw()` and JS `decodeBC3Block()`/`decodeBC1Block()`.
  - BC3 color table was missing the `c0<=c1` branch entirely in the JS decoder — always built the 4-interpolation version. Added proper branch with `(c0+c1)/2` and transparent black.
  - RGB565 color endpoints were read as big-endian (`>H`) but Switch stores them little-endian (`<H`). Fixed in both Python and JS decoders.
- **GOB deswizzle:** Switch uses Tegra X1 block-linear texture storage. A deswizzle function exists in the frontend (`deswizzleGobOffset`) but is not currently applied. Textures may need GOB deswizzling for pixel-perfect decode.
- **No decompiler:** Cannot decompile game Lua bytecode back to source. Only replacement scripting is possible.
- **Animation format:** ClipDataBlock command block encoding partially reverse-engineered. Full channel-to-bone mapping and multi-frame keyframe extraction still in progress.
- **Animation export:** Visual distortion in exported glTF when imported into other tools — all four known issues (inverse bind matrix timing, parent transform accumulation, axis convention, weight normalization) have been fixed.
- **Model replacement:** Only single-mesh glTF imports are supported. Multi-primitive meshes and exact material assignment need testing.

---

## Credits

- **[cars3-blender-io](https://github.com/DJmax0955/cars3-blender-io)** by DJmax0955 - Format reverse-engineering
- **[RevOctane](https://github.com/zzh8829/RevOctane)** by zzh8829 - Octane engine research

---

## License

Open-source, intended for educational and research purposes.
