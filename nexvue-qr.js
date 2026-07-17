/**
 * nexvue-qr.js — minimal byte-mode QR encoder for NexVUE share-to-phone.
 * ECC level M, versions 1–10 (enough for typical https://edge/... URLs).
 * No dependencies. Exposes window.NexVUEQR.render(canvas, text, opts?).
 *
 * Algorithm follows ISO/IEC 18004 byte mode + Reed–Solomon (GF(256)).
 */
(function (global) {
  "use strict";

  // Capacity (data codewords) for ECC-M, versions 1–10
  const DATA_CW = [0, 16, 28, 44, 64, 86, 108, 124, 154, 182, 216];
  // ECC codewords per block / number of blocks (ECC-M)
  const ECC_CW = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26];
  const ECC_BLOCKS = [0, 1, 1, 1, 2, 2, 4, 4, 4, 4, 6];

  const EXP = new Uint8Array(512);
  const LOG = new Uint8Array(256);
  (function initGF() {
    let x = 1;
    for (let i = 0; i < 255; i++) {
      EXP[i] = x;
      LOG[x] = i;
      x <<= 1;
      if (x & 0x100) x ^= 0x11d;
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  })();

  function gfMul(a, b) {
    if (!a || !b) return 0;
    return EXP[LOG[a] + LOG[b]];
  }

  function rsGenerator(ecLen) {
    let g = [1];
    for (let i = 0; i < ecLen; i++) {
      const next = new Array(g.length + 1).fill(0);
      for (let j = 0; j < g.length; j++) {
        next[j] ^= g[j];
        next[j + 1] ^= gfMul(g[j], EXP[i]);
      }
      g = next;
    }
    return g;
  }

  function rsEncode(data, ecLen) {
    const gen = rsGenerator(ecLen);
    const res = new Array(ecLen).fill(0);
    for (let i = 0; i < data.length; i++) {
      const factor = data[i] ^ res[0];
      res.shift();
      res.push(0);
      if (!factor) continue;
      for (let j = 0; j < ecLen; j++) {
        res[j] ^= gfMul(gen[j + 1] || 0, factor);
      }
    }
    return res;
  }

  function bitBuffer() {
    const bits = [];
    return {
      put(val, len) {
        for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1);
      },
      toBytes() {
        const out = [];
        for (let i = 0; i < bits.length; i += 8) {
          let b = 0;
          for (let j = 0; j < 8; j++) b = (b << 1) | (bits[i + j] || 0);
          out.push(b);
        }
        return out;
      },
      length() { return bits.length; },
    };
  }

  function chooseVersion(byteLen) {
    for (let v = 1; v <= 10; v++) {
      const bitsNeeded = 4 + (v <= 9 ? 8 : 16) + byteLen * 8 + 4;
      const capacityBits = DATA_CW[v] * 8;
      if (bitsNeeded <= capacityBits) return v;
    }
    throw new Error("NexVUEQR: text too long for versions 1–10");
  }

  function encodeData(text, version) {
    const bytes = Array.from(new TextEncoder().encode(text));
    const buf = bitBuffer();
    buf.put(0b0100, 4); // byte mode
    buf.put(bytes.length, version <= 9 ? 8 : 16);
    for (const b of bytes) buf.put(b, 8);
    const capacity = DATA_CW[version] * 8;
    const remain = capacity - buf.length();
    if (remain > 4) buf.put(0, 4);
    else if (remain > 0) buf.put(0, remain);
    while (buf.length() % 8) buf.put(0, 1);
    const data = buf.toBytes();
    const pad = [0xec, 0x11];
    let pi = 0;
    while (data.length < DATA_CW[version]) data.push(pad[pi++ % 2]);
    return data;
  }

  function interleave(data, version) {
    const nBlocks = ECC_BLOCKS[version];
    const ecLen = ECC_CW[version];
    const totalData = DATA_CW[version];
    const shortBlocks = nBlocks - (totalData % nBlocks);
    const shortLen = Math.floor(totalData / nBlocks);
    const longLen = shortLen + 1;
    const blocks = [];
    let offset = 0;
    for (let i = 0; i < nBlocks; i++) {
      const len = i < shortBlocks ? shortLen : longLen;
      const slice = data.slice(offset, offset + len);
      offset += len;
      blocks.push({ data: slice, ecc: rsEncode(slice, ecLen) });
    }
    const out = [];
    const maxData = Math.max(...blocks.map(b => b.data.length));
    for (let i = 0; i < maxData; i++) {
      for (const b of blocks) if (i < b.data.length) out.push(b.data[i]);
    }
    for (let i = 0; i < ecLen; i++) {
      for (const b of blocks) out.push(b.ecc[i]);
    }
    return out;
  }

  function moduleSize(version) {
    return 17 + 4 * version;
  }

  function makeMatrix(version) {
    const n = moduleSize(version);
    const m = Array.from({ length: n }, () => new Array(n).fill(null));
    function fillFinder(r, c) {
      for (let y = -1; y <= 7; y++) {
        for (let x = -1; x <= 7; x++) {
          const rr = r + y, cc = c + x;
          if (rr < 0 || cc < 0 || rr >= n || cc >= n) continue;
          const on = (x >= 0 && x <= 6 && y >= 0 && y <= 6) &&
            (x === 0 || x === 6 || y === 0 || y === 6 || (x >= 2 && x <= 4 && y >= 2 && y <= 4));
          m[rr][cc] = on;
        }
      }
    }
    fillFinder(0, 0);
    fillFinder(0, n - 7);
    fillFinder(n - 7, 0);
    for (let i = 8; i < n - 8; i++) {
      m[6][i] = m[6][i] === null ? (i % 2 === 0) : m[6][i];
      m[i][6] = m[i][6] === null ? (i % 2 === 0) : m[i][6];
    }
    // alignment patterns (versions 2+)
    if (version >= 2) {
      const centers = alignmentCenters(version);
      for (const r of centers) {
        for (const c of centers) {
          if (m[r][c] !== null) continue;
          for (let y = -2; y <= 2; y++) {
            for (let x = -2; x <= 2; x++) {
              m[r + y][c + x] =
                Math.max(Math.abs(x), Math.abs(y)) !== 1 && !(x === 0 && y === 0)
                  ? (Math.max(Math.abs(x), Math.abs(y)) === 2 || (x === 0 && y === 0))
                  : false;
              if (Math.max(Math.abs(x), Math.abs(y)) === 1) m[r + y][c + x] = false;
              if (x === 0 && y === 0) m[r + y][c + x] = true;
              if (Math.max(Math.abs(x), Math.abs(y)) === 2) m[r + y][c + x] = true;
            }
          }
          // rewrite cleanly
          for (let y = -2; y <= 2; y++) {
            for (let x = -2; x <= 2; x++) {
              const d = Math.max(Math.abs(x), Math.abs(y));
              m[r + y][c + x] = d === 0 || d === 2;
            }
          }
        }
      }
    }
    // dark module + format/version placeholders reserved as false temporarily
    m[n - 8][8] = true;
    for (let i = 0; i < 8; i++) {
      if (m[8][i] === null) m[8][i] = false;
      if (m[i][8] === null) m[i][8] = false;
      if (m[8][n - 1 - i] === null) m[8][n - 1 - i] = false;
      if (m[n - 1 - i][8] === null) m[n - 1 - i][8] = false;
    }
    if (m[8][8] === null) m[8][8] = false;
    return m;
  }

  function alignmentCenters(version) {
    // Sufficient for v2–10
    const table = {
      2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
      6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
      9: [6, 26, 46], 10: [6, 28, 50],
    };
    return table[version] || [6];
  }

  function maskFn(id, r, c) {
    switch (id) {
      case 0: return (r + c) % 2 === 0;
      case 1: return r % 2 === 0;
      case 2: return c % 3 === 0;
      case 3: return (r + c) % 3 === 0;
      case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
      case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
      case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
      case 7: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
      default: return false;
    }
  }

  function placeData(matrix, data) {
    const n = matrix.length;
    let bitIdx = 0;
    const bits = [];
    for (const b of data) {
      for (let i = 7; i >= 0; i--) bits.push((b >>> i) & 1);
    }
    let upward = true;
    for (let col = n - 1; col > 0; col -= 2) {
      if (col === 6) col--;
      for (let i = 0; i < n; i++) {
        const row = upward ? n - 1 - i : i;
        for (let dx = 0; dx < 2; dx++) {
          const c = col - dx;
          if (matrix[row][c] !== null) continue;
          matrix[row][c] = bits[bitIdx++] === 1;
          if (bitIdx >= bits.length) matrix[row][c] = false;
        }
      }
      upward = !upward;
    }
  }

  function applyMask(matrix, maskId) {
    const n = matrix.length;
    const out = matrix.map(row => row.slice());
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        // only data modules were null before place; after place all filled.
        // Re-detect reserved by regenerating pattern map — simpler: mask only
        // modules that were data: we track via a reserved grid.
      }
    }
    return out;
  }

  function reservedMap(version) {
    const m = makeMatrix(version);
    const n = m.length;
    const reserved = Array.from({ length: n }, () => new Array(n).fill(false));
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (m[r][c] !== null) reserved[r][c] = true;
      }
    }
    return { pattern: m, reserved };
  }

  // BCH for format info
  function formatBits(ecc /* M=00 */, maskId) {
    const eccBits = 0b00; // M
    let data = ((eccBits << 3) | maskId) << 10;
    let rem = data;
    for (let i = 14; i >= 10; i--) {
      if ((rem >>> i) & 1) rem ^= 0x537 << (i - 10);
    }
    return ((eccBits << 3) | maskId) << 10 | rem ^ 0x5412;
  }

  function drawFormat(matrix, maskId) {
    const n = matrix.length;
    const bits = formatBits(0, maskId);
    const positions = [
      // horizontal near finder
      [8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8],
      [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8],
    ];
    // Actually ISO order for format: two copies. Use standard placement:
    const coords1 = [
      [8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7], [8, 8],
      [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8],
    ];
    const coords2 = [
      [n - 1, 8], [n - 2, 8], [n - 3, 8], [n - 4, 8], [n - 5, 8], [n - 6, 8], [n - 7, 8],
      [8, n - 8], [8, n - 7], [8, n - 6], [8, n - 5], [8, n - 4], [8, n - 3], [8, n - 2], [8, n - 1],
    ];
    for (let i = 0; i < 15; i++) {
      const bit = ((bits >>> (14 - i)) & 1) === 1;
      const [r1, c1] = coords1[i];
      matrix[r1][c1] = bit;
      const [r2, c2] = coords2[i];
      matrix[r2][c2] = bit;
    }
  }

  function penalty(matrix) {
    const n = matrix.length;
    let score = 0;
    // N1: runs
    for (let r = 0; r < n; r++) {
      let run = 1;
      for (let c = 1; c < n; c++) {
        if (matrix[r][c] === matrix[r][c - 1]) {
          run++;
          if (run === 5) score += 3;
          else if (run > 5) score++;
        } else run = 1;
      }
    }
    for (let c = 0; c < n; c++) {
      let run = 1;
      for (let r = 1; r < n; r++) {
        if (matrix[r][c] === matrix[r - 1][c]) {
          run++;
          if (run === 5) score += 3;
          else if (run > 5) score++;
        } else run = 1;
      }
    }
    // N2: 2x2 blocks
    for (let r = 0; r < n - 1; r++) {
      for (let c = 0; c < n - 1; c++) {
        const v = matrix[r][c];
        if (v === matrix[r][c + 1] && v === matrix[r + 1][c] && v === matrix[r + 1][c + 1]) {
          score += 3;
        }
      }
    }
    // N4: balance
    let dark = 0;
    for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (matrix[r][c]) dark++;
    const pct = (dark * 100) / (n * n);
    score += Math.floor(Math.abs(pct - 50) / 5) * 10;
    return score;
  }

  function buildMatrix(text) {
    const bytes = Array.from(new TextEncoder().encode(text));
    const version = chooseVersion(bytes.length);
    const data = interleave(encodeData(text, version), version);
    const { pattern, reserved } = reservedMap(version);
    const n = pattern.length;

    // place raw bits into a working copy (nulls are data)
    const base = pattern.map(row => row.slice());
    placeData(base, data);

    let best = null;
    let bestScore = Infinity;
    for (let mask = 0; mask < 8; mask++) {
      const cand = base.map(row => row.slice());
      for (let r = 0; r < n; r++) {
        for (let c = 0; c < n; c++) {
          if (reserved[r][c]) continue;
          if (maskFn(mask, r, c)) cand[r][c] = !cand[r][c];
        }
      }
      drawFormat(cand, mask);
      const score = penalty(cand);
      if (score < bestScore) {
        bestScore = score;
        best = cand;
      }
    }
    return best;
  }

  function render(canvas, text, opts) {
    const matrix = buildMatrix(String(text));
    const n = matrix.length;
    const scale = (opts && opts.scale) || 6;
    const margin = (opts && opts.margin != null) ? opts.margin : 2;
    const size = (n + margin * 2) * scale;
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = (opts && opts.light) || "#ffffff";
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = (opts && opts.dark) || "#0c0f12";
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (!matrix[r][c]) continue;
        ctx.fillRect((c + margin) * scale, (r + margin) * scale, scale, scale);
      }
    }
    return canvas;
  }

  global.NexVUEQR = { render, buildMatrix };
})(typeof window !== "undefined" ? window : globalThis);
