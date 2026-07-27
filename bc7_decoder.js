function decodeBC7Texture(data, width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const imageData = ctx.createImageData(width, height);
  const pixels = imageData.data;
  const blocksX = width >> 2;
  const blocksY = height >> 2;
  const T2 = [0, 85, 170, 255];
  const T3 = [0, 36, 73, 109, 146, 182, 219, 255];
  const T4 = [0, 17, 34, 51, 68, 85, 102, 119, 136, 153, 170, 187, 204, 221, 238, 255];
  function readBits(block, offset, count) {
    let result = 0;
    for (let i = 0; i < count; i++) {
      const byteIdx = (offset + i) >> 3;
      const bitIdx = (offset + i) & 7;
      if (byteIdx < 16) {
        result |= ((block[byteIdx] >> (7 - bitIdx)) & 1) << (count - 1 - i);
      }
    }
    return result;
  }
  function unorm8(value, bits) {
    if (bits === 8) return value;
    if (bits === 7) return (value << 1) | (value >>> 6);
    if (bits === 6) return (value << 2) | (value >>> 4);
    if (bits === 5) return (value << 3) | (value >>> 2);
    if (bits === 4) return (value << 4) | value;
    if (bits === 3) return value << 5;
    if (bits === 2) return value * 85;
    return value;
  }
  function unorm8P(value, bits, p) {
    return unorm8((value << 1) | p, bits + 1);
  }
  function interp(a, b, t, idxBits) {
    const w = idxBits === 4 ? T4[t] : idxBits === 3 ? T3[t] : T2[t];
    return ((a * (64 - w) + b * w + 32) >>> 6) & 0xff;
  }
  const P2 = new Uint16Array([
    0xCCCC, 0xCCCC, 0x3333, 0x3333, 0xCC33, 0xCC33, 0x33CC, 0x33CC,
    0x00CC, 0x00CC, 0xCC00, 0xCC00, 0x0CCC, 0x0CCC, 0xCCC0, 0xCCC0,
    0x000C, 0x000C, 0x00C0, 0x00C0, 0x0C00, 0x0C00, 0xC000, 0xC000,
    0x0C0C, 0x0C0C, 0xC0C0, 0xC0C0, 0x0CC0, 0x0CC0, 0xCC0C, 0xCC0C,
    0x000F, 0x000F, 0xF000, 0xF000, 0x0FF0, 0x0FF0, 0xF00F, 0xF00F,
    0x0F0F, 0x0F0F, 0xF0F0, 0xF0F0, 0x00FF, 0x00FF, 0xFF00, 0xFF00,
    0x0F00, 0x0F00, 0xF000, 0xF000, 0x00F0, 0x00F0, 0x000F, 0x000F,
    0x00FF, 0x00FF, 0xFF00, 0xFF00, 0x0F0F, 0x0F0F, 0xF0F0, 0xF0F0
  ]);
  for (let by = 0; by < blocksY; by++) {
    for (let bx = 0; bx < blocksX; bx++) {
      const blockOffset = (by * blocksX + bx) * 16;
      const block = data.subarray(blockOffset, blockOffset + 16);
      const mode = readBits(block, 0, 3);
      let subsetCount = 1;
      let partitionBits = 0;
      let anchorBits = 0;
      let r0bits = 0, g0bits = 0, b0bits = 0, a0bits = 0;
      let r1bits = 0, g1bits = 0, b1bits = 0, a1bits = 0;
      let hasP0 = false, hasP1 = false, hasPA0 = false, hasPA1 = false;
      let hasPShared = false;
      let indexBits = 3;
      let ism = 0;
      switch (mode) {
        case 0: subsetCount=2; partitionBits=6; r0bits=4; g0bits=4; b0bits=4; r1bits=4; g1bits=4; b1bits=4; a0bits=8; a1bits=8; hasPShared=true; anchorBits=1; indexBits=3; break;
        case 1: subsetCount=2; partitionBits=6; r0bits=6; g0bits=6; b0bits=6; r1bits=6; g1bits=6; b1bits=6; a0bits=2; a1bits=2; anchorBits=1; indexBits=3; break;
        case 2: subsetCount=1; r0bits=5; g0bits=5; b0bits=5; r1bits=5; g1bits=5; b1bits=5; a0bits=0; a1bits=0; indexBits=3; break;
        case 3: subsetCount=1; r0bits=4; g0bits=4; b0bits=4; r1bits=4; g1bits=4; b1bits=4; a0bits=4; a1bits=4; hasP0=true; hasPA0=true; hasPA1=true; anchorBits=1; indexBits=3; break;
        case 4: subsetCount=1; r0bits=5; g0bits=6; b0bits=5; r1bits=5; g1bits=6; b1bits=5; a0bits=6; a1bits=6; hasP0=true; hasPA0=true; hasPA1=true; ism=1; indexBits=2; break;
        case 5: subsetCount=1; r0bits=6; g0bits=6; b0bits=6; r1bits=6; g1bits=6; b1bits=6; a0bits=6; a1bits=6; hasP0=true; hasPA0=true; hasPA1=true; ism=1; anchorBits=1; indexBits=2; break;
        case 6: subsetCount=1; r0bits=7; g0bits=7; b0bits=7; r1bits=7; g1bits=7; b1bits=7; a0bits=7; a1bits=7; hasP0=true; hasPA0=true; hasPA1=true; anchorBits=1; indexBits=4; break;
        case 7: subsetCount=2; partitionBits=6; r0bits=4; g0bits=4; b0bits=4; r1bits=4; g1bits=4; b1bits=4; a0bits=6; a1bits=5; hasP0=true; anchorBits=1; indexBits=2; break;
        default: continue;
      }
      let bitPtr = 3;
      const partition = partitionBits ? readBits(block, bitPtr, partitionBits) : 0;
      bitPtr += partitionBits;
      let R0 = readBits(block, bitPtr, r0bits); bitPtr += r0bits;
      let G0 = readBits(block, bitPtr, g0bits); bitPtr += g0bits;
      let B0 = readBits(block, bitPtr, b0bits); bitPtr += b0bits;
      let R1 = readBits(block, bitPtr, r1bits); bitPtr += r1bits;
      let G1 = readBits(block, bitPtr, g1bits); bitPtr += g1bits;
      let B1 = readBits(block, bitPtr, b1bits); bitPtr += b1bits;
      let P0 = 0, PR0 = 0, PR1 = 0;
      if (hasPShared) {
        P0 = readBits(block, bitPtr, 1); bitPtr += 1;
        PR0 = P0; PR1 = P0;
      } else if (hasP0 && (mode === 4 || mode === 5)) {
        PR0 = readBits(block, bitPtr, 1); bitPtr += 1;
        PR1 = readBits(block, bitPtr, 1); bitPtr += 1;
      } else if (hasP0) {
        P0 = readBits(block, bitPtr, 1); bitPtr += 1;
      }
      let A0raw = 0, A1raw = 0;
      if (a0bits > 0) {
        A0raw = readBits(block, bitPtr, a0bits); bitPtr += a0bits;
      }
      let PA0 = 0;
      if (hasPA0) { PA0 = readBits(block, bitPtr, 1); bitPtr += 1; }
      if (a1bits > 0) {
        A1raw = readBits(block, bitPtr, a1bits); bitPtr += a1bits;
      }
      let PA1 = 0;
      if (hasPA1) { PA1 = readBits(block, bitPtr, 1); bitPtr += 1; }
      let A2raw = 0, A3raw = 0, PA7 = 0;
      if (mode === 7) {
        A2raw = readBits(block, bitPtr, 4); bitPtr += 4;
        A3raw = readBits(block, bitPtr, 3); bitPtr += 3;
        PA7 = readBits(block, bitPtr, 1); bitPtr += 1;
      }
      let A0 = 0, A1 = 0;
      if (mode === 0 || mode === 1) {
        A0 = unorm8(A0raw, a0bits);
        A1 = unorm8(A1raw, a1bits);
      } else if (mode === 3) {
        A0 = unorm8P(A0raw, a0bits, PA0);
        A1 = unorm8P(A1raw, a1bits, PA1);
      } else if (mode === 4 || mode === 5) {
        A0 = unorm8P(A0raw, a0bits, PA0);
        A1 = unorm8P(A1raw, a1bits, PA1);
      } else if (mode === 6) {
        A0 = unorm8P(A0raw, a0bits, PA0);
        A1 = unorm8P(A1raw, a1bits, PA1);
      } else if (mode === 7) {
        A0 = unorm8(A0raw, 6);
        A1 = unorm8(A1raw, 5);
      }
      let E0r, E0g, E0b, E1r, E1g, E1b;
      if (hasPShared) {
        E0r = unorm8P(R0, r0bits, P0);
        E0g = unorm8P(G0, g0bits, P0);
        E0b = unorm8P(B0, b0bits, P0);
        E1r = unorm8P(R1, r1bits, P0);
        E1g = unorm8P(G1, g1bits, P0);
        E1b = unorm8P(B1, b1bits, P0);
      } else if (hasP0 && (mode === 4 || mode === 5)) {
        E0r = unorm8P(R0, r0bits, PR0);
        E0g = unorm8P(G0, g0bits, PR0);
        E0b = unorm8P(B0, b0bits, PR0);
        E1r = unorm8P(R1, r1bits, PR1);
        E1g = unorm8P(G1, g1bits, PR1);
        E1b = unorm8P(B1, b1bits, PR1);
      } else if (hasP0) {
        E0r = unorm8P(R0, r0bits, P0);
        E0g = unorm8P(G0, g0bits, P0);
        E0b = unorm8P(B0, b0bits, P0);
        E1r = unorm8P(R1, r1bits, P0);
        E1g = unorm8P(G1, g1bits, P0);
        E1b = unorm8P(B1, b1bits, P0);
      } else {
        E0r = unorm8(R0, r0bits);
        E0g = unorm8(G0, g0bits);
        E0b = unorm8(B0, b0bits);
        E1r = unorm8(R1, r1bits);
        E1g = unorm8(G1, g1bits);
        E1b = unorm8(B1, b1bits);
      }
      const endpoints = [[E0r, E0g, E0b, A0], [E1r, E1g, E1b, A1]];
      if (subsetCount === 2 && mode === 7) {
        endpoints.push([unorm8P(R0, 4, P0), unorm8P(G0, 4, P0), unorm8P(B0, 4, P0), A0]);
        endpoints.push([unorm8P(R1, 4, P0), unorm8P(G1, 4, P0), unorm8P(B1, 4, P0), A1]);
      }
      let idxBits = indexBits;
      const colorIndices = new Uint8Array(16);
      for (let p = 0; p < 16; p++) {
        let pBits = idxBits;
        if (p === 0 && anchorBits) pBits = anchorBits;
        colorIndices[p] = readBits(block, bitPtr, pBits);
        bitPtr += pBits;
      }
      let alphaIndices = null;
      if (mode === 0 || mode === 1) {
        alphaIndices = new Uint8Array(16);
        for (let p = 0; p < 16; p++) {
          let pBits = 3;
          if (p === 0 && anchorBits) pBits = anchorBits;
          alphaIndices[p] = readBits(block, bitPtr, pBits);
          bitPtr += pBits;
        }
      }
      for (let p = 0; p < 16; p++) {
        const px = p & 3;
        const py = p >> 2;
        const subset = subsetCount > 1 ? ((P2[partition] >> (15 - p)) & 1) : 0;
        let c0, c1;
        if (subsetCount === 2) {
          if (mode === 7) {
            c0 = endpoints[subset];
            c1 = endpoints[subset + 2];
          } else {
            c0 = endpoints[subset];
            c1 = endpoints[subset === 0 ? 1 : 0];
          }
        } else {
          c0 = endpoints[0];
          c1 = endpoints[1];
        }
        let ib = idxBits;
        if (p === 0 && anchorBits) ib = anchorBits;
        const r = interp(c0[0], c1[0], colorIndices[p], ib);
        const g = interp(c0[1], c1[1], colorIndices[p], ib);
        const b = interp(c0[2], c1[2], colorIndices[p], ib);
        let a;
        if (mode === 2) {
          a = 255;
        } else if (alphaIndices) {
          a = interp(c0[3], c1[3], alphaIndices[p], ib);
        } else {
          a = interp(c0[3], c1[3], colorIndices[p], ib);
        }
        const dstX = bx * 4 + px;
        const dstY = by * 4 + py;
        const dstIdx = (dstY * width + dstX) * 4;
        pixels[dstIdx] = r;
        pixels[dstIdx + 1] = g;
        pixels[dstIdx + 2] = b;
        pixels[dstIdx + 3] = a;
      }
    }
  }
  ctx.putImageData(imageData, 0, 0);
  return canvas;
}
