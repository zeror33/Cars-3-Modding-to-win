#!/usr/bin/env python3
"""Run the cars3_viewer Python server briefly and capture all API responses as static JSON files."""
import os, json, sys, time, urllib.request, urllib.error, threading

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, 'RevOctane'))
sys.path.insert(0, BASE)

# Import server code
import cars3_viewer

STATIC_DIR = os.path.join(BASE, 'static_api')
os.makedirs(STATIC_DIR, exist_ok=True)

def start_server():
    server = cars3_viewer.ThreadedHTTPServer(('127.0.0.1', 8766), cars3_viewer.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(1)
    return server

def fetch(path):
    url = f'http://127.0.0.1:8766{path}'
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'  FAILED: {path} — {e}')
        return None

def save(filename, data):
    path = os.path.join(STATIC_DIR, filename)
    with open(path, 'w') as f:
        json.dump(data, f)
    size = os.path.getsize(path)
    print(f'  Saved {filename} ({size:,} bytes)')

print('Starting server...')
server = start_server()
time.sleep(1)

try:
    # 1. Character list
    print('\n--- /api/characters ---')
    chars = fetch('/api/characters')
    if chars:
        save('characters.json', chars)
        char_list = chars.get('characters', [])
        print(f'  {len(char_list)} characters found')
    
    # 2. Asset list
    print('\n--- /api/assets ---')
    assets = fetch('/api/assets')
    if assets:
        save('assets.json', assets)
        cats = assets.get('categories', {})
        total = sum(len(v) for v in cats.values())
        print(f'  {total} assets across {len(cats)} categories')
    
    # 3. Character detail for each character
    if chars:
        char_list = chars.get('characters', [])
        for i, c in enumerate(char_list):
            cid = c['id']
            print(f'\n--- /api/character?id={cid} ({i+1}/{len(char_list)}) ---')
            data = fetch(f'/api/character?id={cid}')
            if data and 'groups' in data:
                # Remove png_data to save space (JS will decode from raw data)
                for t in data.get('textures', []):
                    t.pop('png_data', None)
                save(f'character_{cid}.json', data)
                groups = len(data.get('groups', []))
                textures = len(data.get('textures', []))
                size = os.path.getsize(os.path.join(STATIC_DIR, f'character_{cid}.json'))
                print(f'  {groups} groups, {textures} textures, {size:,} bytes')
    
    # 4. Scripts list
    print('\n--- /api/scripts ---')
    scripts = fetch('/api/scripts')
    if scripts:
        save('scripts.json', scripts)

finally:
    server.shutdown()
    print('\nDone! Static files in static_api/')
