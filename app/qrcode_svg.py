"""Gerador compacto de QR Code em SVG (ISO/IEC 18004, modo Byte, ECC L) usando apenas stdlib."""

# Capacidade máxima de bytes no modo Byte (nível L) e parâmetros de bloco por versão (1 a 6)
# (total_codewords, ec_codewords_per_block, num_blocks, max_bytes)
_VERSIONS = {
    1: (26, 7, 1, 17),
    2: (44, 10, 1, 32),
    3: (70, 15, 1, 53),
    4: (100, 20, 1, 78),
    5: (134, 26, 1, 106),
    6: (172, 18, 2, 134),
}

_ALIGNMENT_POS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
}


def _gf_tables() -> tuple[list[int], list[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        next_poly = [0] * (len(poly) + 1)
        factor = _GF_EXP[i]
        for j, coeff in enumerate(poly):
            next_poly[j] ^= coeff
            next_poly[j + 1] ^= _gf_mul(coeff, factor)
        poly = next_poly
    return poly


def _rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = _rs_generator(ec_len)
    reg = [0] * ec_len
    for byte in data:
        factor = byte ^ reg[0]
        reg = reg[1:] + [0]
        if factor != 0:
            for i in range(ec_len):
                reg[i] ^= _gf_mul(gen[i + 1], factor)
    return reg


def _encode_codewords(payload: bytes) -> tuple[int, list[int]]:
    length = len(payload)
    version = next((v for v, spec in _VERSIONS.items() if length <= spec[3]), 6)
    if length > _VERSIONS[version][3]:
        payload = payload[: _VERSIONS[version][3]]
        length = len(payload)
    total_cw, ec_cw, blocks, _ = _VERSIONS[version]
    data_cw = total_cw - ec_cw * blocks

    bits: list[int] = []

    def push_bits(val: int, count: int) -> None:
        for i in range(count - 1, -1, -1):
            bits.append((val >> i) & 1)

    push_bits(0b0100, 4)  # Modo Byte
    push_bits(length, 8)
    for b in payload:
        push_bits(b, 8)
    for _ in range(min(4, data_cw * 8 - len(bits))):
        bits.append(0)
    while len(bits) % 8 != 0:
        bits.append(0)

    codewords = [
        sum(bits[i + j] << (7 - j) for j in range(8))
        for i in range(0, len(bits), 8)
    ]
    pad = [0xEC, 0x11]
    idx = 0
    while len(codewords) < data_cw:
        codewords.append(pad[idx % 2])
        idx += 1

    block_size = data_cw // blocks
    data_blocks = [
        codewords[i * block_size : (i + 1) * block_size] for i in range(blocks)
    ]
    ec_blocks = [_rs_encode(block, ec_cw) for block in data_blocks]

    interleaved: list[int] = []
    for col in range(block_size):
        for b in range(blocks):
            interleaved.append(data_blocks[b][col])
    for col in range(ec_cw):
        for b in range(blocks):
            interleaved.append(ec_blocks[b][col])
    return version, interleaved


def _build_matrix(version: int, codewords: list[int]) -> list[list[int]]:
    size = 17 + 4 * version
    matrix: list[list[int | None]] = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(r: int, c: int, val: int, reserve: bool = True) -> None:
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = val
            if reserve:
                reserved[r][c] = True

    def place_finder(top: int, left: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = top + r, left + c
                if 0 <= rr < size and 0 <= cc < size:
                    in_outer = 0 <= r <= 6 and (r in (0, 6) or c in (0, 6)) and 0 <= c <= 6
                    in_inner = 2 <= r <= 4 and 2 <= c <= 4
                    set_module(rr, cc, 1 if (in_outer or in_inner) else 0, True)

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    for i in range(8, size - 8):
        set_module(6, i, 1 if i % 2 == 0 else 0, True)
        set_module(i, 6, 1 if i % 2 == 0 else 0, True)

    coords = _ALIGNMENT_POS[version]
    for r in coords:
        for c in coords:
            if reserved[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    val = 1 if max(abs(dr), abs(dc)) in (0, 2) else 0
                    set_module(r + dr, c + dc, val, True)

    set_module(4 * version + 9, 8, 1, True)
    for i in range(9):
        if i < size:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    bits = [(byte >> i) & 1 for byte in codewords for i in range(7, -1, -1)]
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        row_range = range(size - 1, -1, -1) if upward else range(size)
        for row in row_range:
            for dc in (0, -1):
                c = col + dc
                if not reserved[row][c]:
                    bit = bits[bit_idx] if bit_idx < len(bits) else 0
                    bit_idx += 1
                    if (row + c) % 2 == 0:
                        bit ^= 1
                    matrix[row][c] = bit
        upward = not upward
        col -= 2

    # Format info para ECC L (01) + máscara 0 (000) => 0x77C4
    fmt_bits = 0x77C4
    fmt = [(fmt_bits >> i) & 1 for i in range(14, -1, -1)]
    pos_r = [8] * 6 + [8, 8, 7] + list(range(5, -1, -1))
    pos_c = list(range(0, 6)) + [7, 8, 8] + [8] * 6
    for i, bit in enumerate(fmt):
        matrix[pos_r[i]][pos_c[i]] = bit
    for i in range(7):
        matrix[size - 1 - i][8] = fmt[i]
    for i in range(7, 15):
        matrix[8][size - 15 + i] = fmt[i]

    return [[int(cell or 0) for cell in row] for row in matrix]


def render_qr_svg(text: str, *, border: int = 2) -> str:
    payload = (text or "").strip().encode("utf-8") or b"http://localhost:8000/enviar"
    version, codewords = _encode_codewords(payload)
    matrix = _build_matrix(version, codewords)
    n = len(matrix)
    dim = n + border * 2
    rects: list[str] = []
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                rects.append(f"M{c + border},{r + border}h1v1h-1z")
    path_d = "".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR Code">'
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
        f'<path d="{path_d}" fill="#174d3d"/></svg>'
    )
