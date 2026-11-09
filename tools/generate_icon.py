"""从胶片 SVG 的同一几何形状独立渲染各尺寸，无第三方绘图依赖。"""
from pathlib import Path
import struct
import zlib

ASSETS = Path(__file__).resolve().parents[1] / 'frontend' / 'assets'
SIZES = (16, 20, 24, 28, 32, 40, 48, 56, 64, 72, 80, 96, 128, 256)


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def render(size):
    scale = size / 64
    edge = lambda value: round(value * scale)
    left, top, right, bottom = map(edge, (11, 5, 53, 59))
    radius = max(1, edge(6))
    white = [tuple(map(edge, rect)) for rect in [(25, 12, 39, 52)] +
             [(x, y, x+6, y+5) for x in (15, 43) for y in (13, 24, 35, 46)]]
    tx, ty, tr, tb = map(edge, (28, 24, 37, 40))
    middle = (ty + tb) / 2
    raw = bytearray()
    samples = 4
    for y in range(size):
        raw.append(0)
        for x in range(size):
            count = light = 0
            for sy in range(samples):
                py = y + (sy + .5) / samples
                for sx in range(samples):
                    px = x + (sx + .5) / samples
                    if not (left <= px < right and top <= py < bottom):
                        continue
                    dx = max(left + radius - px, 0, px - (right - radius))
                    dy = max(top + radius - py, 0, py - (bottom - radius))
                    if dx * dx + dy * dy > radius * radius:
                        continue
                    count += 1
                    is_white = any(a <= px < c and b <= py < d for a, b, c, d in white)
                    triangle = (ty <= py < tb and tx <= px <= tx + (tr-tx) * (1-abs(py-middle)/(middle-ty)))
                    light += bool(is_white and not triangle)
            alpha = round(255 * count / samples**2)
            gray = round(17 + 238 * light / count) if count else 0
            raw.extend((gray, gray, gray, alpha))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))


def main():
    frames = [(size, render(size)) for size in SIZES]
    header = struct.pack('<HHH', 0, 1, len(frames))
    entries, offset = [], 6 + 16 * len(frames)
    for size, data in frames:
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    ico = header + b''.join(entries) + b''.join(data for _, data in frames)
    (ASSETS / 'studio-film.ico').write_bytes(ico)
    # 新路径同时避开旧版 Windows 任务栏图标资源缓存。
    (ASSETS / 'studio-film-taskbar.ico').write_bytes(ico)
    (ASSETS / 'studio-film.png').write_bytes(frames[-1][1])
    print(f'Generated {len(frames)} icon sizes: {SIZES}')


if __name__ == '__main__':
    main()
