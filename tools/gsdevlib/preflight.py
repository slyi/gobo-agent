"""Offline preflight scan: Scratch compatibility, size and memory, no browser.

Extracted from gsdev.py so the scan can be exercised without the CLI. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


# --- Preflight: Scratch compatibility + memory (offline, no browser) ----------

PREFLIGHT_SCHEMA = 2

# Renderer constants, from scratch-render's SVGSkin/BitmapSkin.
_SVG_MAX_TEXTURE_DIMENSION = 2048
_SVG_BYTES_PER_PIXEL = 12   # texture + per-skin 2D canvas + silhouette
_BITMAP_BYTES_PER_PIXEL = 8  # texture + silhouette (no per-skin canvas)

_PREFLIGHT_DEFAULTS = {
    "deviceMemoryMb": 0,                 # 0 = fixed 2 GB-class budgets; set 2048/3072/4096
                                         # to scale both budgets linearly for larger devices
    "jsonBudget": 5 * 1024 * 1024,      # Scratch's "5 MB" = 5 MiB
    "assetBudget": 10 * 1024 * 1024,    # Scratch's "10 MB" = 10 MiB
    "listLimit": 200_000,
    "memoryBudget": 870 * 1024 ** 2,    # 0.85 GiB: smooth-play target (1056403018 0.65 and
                                        # cropped 957967074 0.80 are smooth; 941463385 1.17 stutters)
    "pixelBudget": 110_000_000,         # smooth-play target (cropped build 104M is smooth)
    "hardMemoryBudget": 1638 * 1024 ** 2,  # 1.6 GiB: measured 2 GB Android crash line
    "hardPixelBudget": 300_000_000,        # measured crash line (52 sheets 277M stutter, 57 304M crash)
    "svgStoredBudget": 100_000_000,     # stored viewBox px; above this, load-time risk, not memory
    "deviceScale": 2,                   # canvas.width / 480; 2 = 2x tablet, raise for phones
    "bitmapBytesPerPx": _BITMAP_BYTES_PER_PIXEL,
    "svgBytesPerPx": _SVG_BYTES_PER_PIXEL,
    "svgPixelWeight": 2,                # plan's observed ~2x SVG-vs-bitmap weight
    "svgMaxWidth": 2400,
    "svgMaxHeight": 1800,
}


def _memory_suggestions(report: dict, budgets: dict) -> list[str]:
    memory = report['memory']
    suggestions = []
    if memory['svgPx'] > 0:
        suggestions.append(
            'reduce SVG area or draw scale (SVG rasterises at the device scale,'
            ' with texture + per-skin canvas + silhouette)')
        suggestions.append('convert SVG costumes to bitmap at the same drawn size'
                           ' (removes the per-skin canvas; fixed at the PNG size)')
    suggestions.append('crop costumes to the visible stage area (transparent margins still cost texels)')
    if memory['bitmapPx'] + memory['svgPx'] > 0:
        suggestions.append('downscale costumes/frames (memory scales with area)')
    if report['sounds']['decodedBytes'] > 0:
        suggestions.append('shorten or drop large sounds (decoded to whole-file PCM in memory)')
    suggestions.append('remove unused costumes and sounds')
    return suggestions


def _budget_text(budget: int) -> str:
    return f"{budget / 2 ** 20:.0f} MiB" if budget < 2 ** 30 else f"{budget / 2 ** 30:g} GiB"


def budgets(get_int) -> dict:
    """Thresholds: ``GSDEV_PREFLIGHT_*`` env -> gobo-agent.json -> conservative default."""
    values = {}
    for key, default in _PREFLIGHT_DEFAULTS.items():
        camel = "preflight" + key[0].upper() + key[1:]
        env = "GSDEV_PREFLIGHT_" + re.sub(r"(?<!^)(?=[A-Z])", "_", key).upper()
        values[key] = get_int(env, camel, default)
    # Device tiering: both budgets scale linearly with the device's RAM when set.
    # 2048 MB -> 1.25 GiB / 200M (the calibrated 2 GB defaults); 1536 -> 960 MiB / 150M.
    memory_mb = values.get("deviceMemoryMb", 0)
    if memory_mb > 0:
        scale = memory_mb / 2048
        values["memoryBudget"] = int(1280 * 1024 ** 2 * scale)
        values["pixelBudget"] = int(200_000_000 * scale)
    return values


def _png_dims(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return None
    return struct.unpack(">II", data[16:24])


def _jpeg_dims(data: bytes):
    i, end = 2, len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > end:
            break
        size = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h)
        if size < 2:
            break
        i += 2 + size
    return None


def _bmp_dims(data: bytes):
    if data[:2] != b"BM" or len(data) < 26:
        return None
    w, h = struct.unpack("<ii", data[18:26])
    return (abs(w), abs(h))


def _gif_dims(data: bytes):
    if data[:3] != b"GIF" or len(data) < 10:
        return None
    return struct.unpack("<HH", data[6:10])


def _webp_dims(data: bytes):
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or len(data) < 30:
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8X":
        return (1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"))
    if fourcc == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF)
    if fourcc == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _avif_dims(data: bytes):
    # AVIF/HEIF are ISO-BMFF: only brand-marked files (not an SVG that happens to
    # contain the bytes "ispe"), then read the ImageSpatialExtents ('ispe') box.
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None
    brand = data[8:12]
    if brand not in (b"avif", b"avis") and b"avif" not in data[8:32] and b"mif1" not in data[8:32]:
        return None
    idx = data.find(b"ispe")
    while idx != -1:
        if idx + 16 <= len(data):
            width, height = struct.unpack(">II", data[idx + 8:idx + 16])
            if 0 < width < 100_000 and 0 < height < 100_000:
                return (width, height)
        idx = data.find(b"ispe", idx + 4)
    return None


def _image_dims(data: bytes, fmt):
    """(kind, dims) by magic bytes, so AVIF-in-a-`.png` etc. is measured correctly."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", _png_dims(data)
    if data[:2] == b"\xff\xd8":
        return "jpg", _jpeg_dims(data)
    if data[:3] == b"GIF":
        return "gif", _gif_dims(data)
    if data[:2] == b"BM":
        return "bmp", _bmp_dims(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", _webp_dims(data)
    if data[4:8] == b"ftyp":
        return "avif", _avif_dims(data)
    if fmt == "svg" or data.lstrip()[:1] == b"<":
        return "svg", _svg_dims(data)
    return (fmt or "?"), None


def _svg_dims(data: bytes) -> dict:
    head = data[:4000]
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        return {"status": "unresolved", "reason": "doctype"}
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {"status": "unresolved", "reason": "parse"}

    def scalar(value):
        if not value:
            return None
        match = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$", value)
        return float(match.group(1)) if match else None

    # The renderer sizes an SVG skin from the viewBox (SVGSkin.setSVG), so use
    # that first; width/height are only a fallback when it is absent.
    elements = sum(1 for _ in root.iter())
    view_box = root.attrib.get("viewBox")
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return {"status": "viewbox", "width": abs(float(parts[2])),
                        "height": abs(float(parts[3])), "elements": elements}
            except ValueError:
                pass
    width, height = scalar(root.attrib.get("width")), scalar(root.attrib.get("height"))
    if width and height:
        return {"status": "px", "width": width, "height": height, "elements": elements}
    return {"status": "unresolved", "reason": "dim"}


def _svg_mip_scale(width: float, height: float, device_scale: float,
                   draw_scale: float = 1.0) -> float:
    """The power-of-two texture scale SVGSkin picks for a drawable at ``draw_scale``.

    ``requestedScale = min(deviceScale * draw_scale, maxTextureScale)``, then rounded
    up to a power of two. ``maxTextureScale`` is the largest power of two with
    ``maxDimension * scale <= 2048``. The result can be below 1 (a sprite authored at
    37.5% uses the 0.5x MIP), down to the renderer's 1/256 floor.
    """
    max_dimension = max(width, height)
    if max_dimension <= 0:
        return 1
    cap = 1
    test = 2
    while max_dimension * test <= _SVG_MAX_TEXTURE_DIMENSION:
        cap = test
        test *= 2
    requested = min(max(device_scale * draw_scale, 1 / 256), cap)
    return max(1 / 256, 2 ** math.ceil(math.log2(requested)))


def _pf(level: str, code: str, message: str, detail=None) -> dict:
    finding = {'level': level, 'code': code, 'message': message}
    if detail is not None:
        finding['detail'] = detail
    return finding


def preflight_scan(sb3_path: Path, budgets: dict) -> dict:
    """Offline Scratch compatibility/size/memory check on an sb3 (no browser)."""
    findings: list[dict] = []
    report = {
        'schema': PREFLIGHT_SCHEMA,
        'artifact': str(sb3_path),
        'sha256': _sha256_file(sb3_path),
        'budgets': budgets,
        'json': {'bytes': 0, 'budget': budgets['jsonBudget'], 'over': False},
        'assets': {'count': 0, 'totalBytes': 0, 'over': [], 'largest': []},
        'lists': {'max': 0, 'over': [], 'atLimit': []},
        'memory': {
            'bitmapPx': 0, 'bitmapTexturePx': 0, 'bitmapSkins': 0,
            'svgPx': 0, 'svgRasterPx': 0, 'svgSkins': 0, 'svgUnresolved': 0,
            'cssMathSkins': 0, 'svgElements': 0, 'maxSvgElements': 0,
            'deviceScale': budgets['deviceScale'],
            'bitmapBytesPerPx': budgets['bitmapBytesPerPx'],
            'svgBytesPerPx': budgets['svgBytesPerPx'],
            'svgPixelWeight': budgets['svgPixelWeight'],
            'effectivePx': 0, 'pixelCost': 0,
            'pixelBudget': budgets['pixelBudget'], 'pixelPercent': 0.0,
            'bytes': 0, 'budget': budgets['memoryBudget'], 'percent': 0.0,
            'overDimension': [], 'largest': [],
        },
        'sounds': {'count': 0, 'bytes': 0, 'decodedBytes': 0, 'unresolved': 0},
        'coverage': {'unresolved': 0},
        'findings': findings,
    }
    try:
        with zipfile.ZipFile(sb3_path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names):
                findings.append(_pf('error', 'duplicate-zip-entries', 'archive has duplicate entry names'))
            if 'project.json' not in names:
                findings.append(_pf('error', 'missing-project-json', 'no project.json in the archive'))
                return report
            project_bytes = archive.read('project.json')
            sizes = {n: archive.getinfo(n).file_size for n in names if n != 'project.json'}
            total_uncompressed = sum(sizes.values()) + len(project_bytes)
            if len(names) > _PREFLIGHT_MAX_ENTRIES or total_uncompressed > _PREFLIGHT_MAX_UNCOMPRESSED:
                findings.append(_pf('error', 'scan-incomplete',
                                    f"archive too large to scan safely "
                                    f"({len(names)} entries, {total_uncompressed:,} bytes uncompressed)"))
                return report
            data = json.loads(project_bytes)
            if not isinstance(data, dict) or 'targets' not in data:
                if isinstance(data, dict) and data.get('objName'):
                    findings.append(_pf('error', 'scratch2-project',
                                        'project.json is a Scratch 2 project; convert it to sb3 '
                                        'before preflight (Scratch 2 costumes are not scanned)'))
                else:
                    findings.append(_pf('error', 'invalid-project',
                                        'project.json has no "targets"; cannot scan this artifact'))
                return report
            targets = data.get('targets', [])

            report['assets']['count'] = len(sizes)
            report['assets']['totalBytes'] = sum(sizes.values())
            over_assets = [{'name': n, 'bytes': s} for n, s in sizes.items() if s > budgets['assetBudget']]
            report['assets']['over'] = sorted(over_assets, key=lambda x: -x['bytes'])[:10]
            report['assets']['largest'] = sorted(
                ({'name': n, 'bytes': s} for n, s in sizes.items()), key=lambda x: -x['bytes']
            )[:10]
            if over_assets:
                findings.append(_pf('error', 'asset-over-budget',
                                    f"{len(over_assets)} asset(s) exceed {budgets['assetBudget']} bytes",
                                    {'assets': report['assets']['over'],
                                     'suggestions': ['compress or downscale the asset below 10 MiB',
                                                     'split a long sound into shorter pieces']}))

            cache: dict[str, dict] = {}
            costume_px: list[tuple] = []
            for target in targets:
                draw_scale = float(target.get('size', 100) or 100) / 100.0
                for costume in target.get('costumes', []):
                    fmt = costume.get('dataFormat')
                    md5ext = costume.get('md5ext') or f"{costume.get('assetId')}.{fmt}"
                    info = cache.get(md5ext)
                    if info is None:
                        info = {'kind': fmt, 'dims': None, 'unresolved': False}
                        try:
                            blob = archive.read(md5ext)
                        except KeyError:
                            info['unresolved'] = True
                        else:
                            kind, dims = _image_dims(blob, fmt)
                            info['kind'] = kind
                            info['cssMath'] = fmt == 'svg' and any(
                                token in blob for token in (b'calc(', b'matrix3d', b'var(--'))
                            if isinstance(dims, dict):
                                if dims.get('status') == 'unresolved':
                                    info['unresolved'] = True
                                else:
                                    info['dims'] = (dims['width'], dims['height'])
                            elif dims:
                                info['dims'] = dims
                            else:
                                info['unresolved'] = True
                            info['elements'] = dims.get('elements', 0) if isinstance(dims, dict) else 0
                        cache[md5ext] = info
                    if info['unresolved'] or not info['dims']:
                        report['coverage']['unresolved'] += 1
                        if fmt == 'svg':
                            report['memory']['svgUnresolved'] += 1
                        continue
                    width, height = info['dims']
                    is_svg = info['kind'] == 'svg'
                    if is_svg and info.get('cssMath'):
                        report['memory']['cssMathSkins'] += 1
                    pixels = int(round(width * height))
                    if is_svg:
                        report['memory']['svgSkins'] += 1
                        report['memory']['svgElements'] += int(info.get('elements', 0) or 0)
                        report['memory']['maxSvgElements'] = max(
                            report['memory']['maxSvgElements'], int(info.get('elements', 0) or 0))
                    else:
                        report['memory']['bitmapSkins'] += 1
                    if is_svg:
                        # Rasterised at the draw scale: viewBox x deviceScale,
                        # rounded up to a power of two and capped at 2048.
                        mip = _svg_mip_scale(width, height, budgets['deviceScale'], draw_scale)
                        texture = int(round(pixels * mip * mip))
                        report['memory']['svgPx'] += pixels
                        report['memory']['svgRasterPx'] += texture
                        if width > budgets['svgMaxWidth'] or height > budgets['svgMaxHeight']:
                            report['memory']['overDimension'].append(
                                {'sprite': target.get('name'), 'costume': costume.get('name'),
                                 'width': width, 'height': height})
                    else:
                        # The stored PNG is the texture. A resolution-1 asset is
                        # upscaled to 2x at load (fetchBitmapCanvas_), which is
                        # 4x the pixels; resolution 2 is used as stored.
                        res = costume.get('bitmapResolution', 1) or 1
                        scale_up = (2.0 / res) if res else 2.0
                        texture = int(round(pixels * scale_up * scale_up))
                        report['memory']['bitmapPx'] += pixels
                        report['memory']['bitmapTexturePx'] += texture
                    costume_px.append({
                        'texture': texture, 'encoded': pixels, 'sprite': target.get('name'),
                        'costume': costume.get('name'), 'kind': info['kind'],
                    })

            for target in targets:
                for entry in target.get('lists', {}).values():
                    count = len(entry[1]) if isinstance(entry[1], list) else 0
                    report['lists']['max'] = max(report['lists']['max'], count)
                    if count > budgets['listLimit']:
                        report['lists']['over'].append(
                            {'sprite': target.get('name'), 'list': entry[0], 'items': count})
                    elif count == budgets['listLimit']:
                        report['lists']['atLimit'].append(
                            {'sprite': target.get('name'), 'list': entry[0], 'items': count})
            if report['lists']['over']:
                findings.append(_pf('error', 'list-over-capacity',
                                    f"list(s) exceed {budgets['listLimit']} items", report['lists']['over']))
            if report['lists']['atLimit']:
                findings.append(_pf('warning', 'list-at-capacity',
                                    f"list(s) at the {budgets['listLimit']}-item limit", report['lists']['atLimit']))

            for target in targets:
                for sound in target.get('sounds', []):
                    fmt = sound.get('dataFormat')
                    md5ext = sound.get('md5ext') or f"{sound.get('assetId')}.{fmt}"
                    report['sounds']['count'] += 1
                    report['sounds']['bytes'] += sizes.get(md5ext, 0)
                    try:
                        blob = archive.read(md5ext)
                    except KeyError:
                        report['sounds']['unresolved'] += 1
                        continue
                    pcm = _sound_pcm_bytes(blob, fmt, len(blob))
                    if pcm is None:
                        report['sounds']['unresolved'] += 1
                    else:
                        report['sounds']['decodedBytes'] += pcm

        report['json']['bytes'] = len(project_bytes)
        report['json']['over'] = len(project_bytes) > budgets['jsonBudget']
        if budgets['jsonBudget']:
            report['json']['percent'] = round(len(project_bytes) / budgets['jsonBudget'] * 100, 1)
        if report['json']['over']:
            findings.append(_pf('error', 'json-over-budget',
                                f"project.json is {len(project_bytes):,} bytes > {budgets['jsonBudget']:,}",
                                {'suggestions': [
                                    'clean up lists: drop duplicate/unused list data and list-monitor entries',
                                    'remove redundant shadow blocks (unused menu/shadow inputs) from project.json',
                                    'shorten large list literals / long strings',
                                    'remove unused blocks, variables and lists',
                                    'move big data into assets instead of project.json',
                                ]}))
        elif len(project_bytes) >= budgets['jsonBudget'] * 9 // 10:
            findings.append(_pf('warning', 'json-near-budget',
                                f"project.json is {len(project_bytes):,} bytes "
                                f"(~{report['json']['percent']}% of {budgets['jsonBudget']:,})"))
        report['memory']['largest'] = [
            {'texturePx': item['texture'], 'encodedPx': item['encoded'],
             'sprite': item['sprite'], 'costume': item['costume'], 'kind': item['kind']}
            for item in sorted(costume_px, key=lambda x: -x['texture'])[:10]
        ]
        memory = report['memory']
        memory['effectivePx'] = memory['bitmapTexturePx'] + memory['svgRasterPx']
        memory['pixelCost'] = int(memory['bitmapPx'] + budgets['svgPixelWeight'] * memory['svgRasterPx'])
        memory['bytes'] = int(
            memory['bitmapTexturePx'] * budgets['bitmapBytesPerPx']
            + memory['svgRasterPx'] * budgets['svgBytesPerPx']
            + report['sounds']['decodedBytes'])
        memory['percent'] = round(memory['bytes'] / budgets['memoryBudget'] * 100, 1) if budgets['memoryBudget'] else 0.0
        memory['pixelPercent'] = round(memory['pixelCost'] / budgets['pixelBudget'] * 100, 1) if budgets['pixelBudget'] else 0.0
        hard_mem = max(budgets.get('hardMemoryBudget', 0), budgets['memoryBudget'])
        hard_px = max(budgets.get('hardPixelBudget', 0), budgets['pixelBudget'])
        if memory['bytes'] > hard_mem:
            findings.append(_pf('error', 'memory-over-budget',
                f"estimated renderer memory (all costumes loaded) ~{memory['bytes'] / 2 ** 30:.2f} GiB "
                f"({memory['percent']}% of the {_budget_text(budgets['memoryBudget'])} target; "
                f"hard limit {_budget_text(hard_mem)})",
                {'bitmapPx': memory['bitmapPx'], 'bitmapTexturePx': memory['bitmapTexturePx'],
                 'svgPx': int(memory['svgPx']), 'svgRasterPx': memory['svgRasterPx'],
                 'deviceScale': budgets['deviceScale'],
                 'bitmapBytesPerPx': budgets['bitmapBytesPerPx'],
                 'svgBytesPerPx': budgets['svgBytesPerPx'],
                 'soundPcmBytes': report['sounds']['decodedBytes'],
                 'suggestions': _memory_suggestions(report, budgets)}))
        elif memory['bytes'] > budgets['memoryBudget']:
            findings.append(_pf('error', 'memory-over-target',
                f"estimated renderer memory ~{memory['bytes'] / 2 ** 30:.2f} GiB "
                f"({memory['percent']}% of the {_budget_text(budgets['memoryBudget'])} smooth target); "
                'above the smooth-play target, will stutter',
                {'bitmapPx': memory['bitmapPx'], 'svgRasterPx': memory['svgRasterPx'],
                 'soundPcmBytes': report['sounds']['decodedBytes']}))
        if memory['pixelCost'] > hard_px:
            findings.append(_pf('error', 'pixel-over-budget',
                f"project-wide costume pixels ~{memory['pixelCost']:,} "
                f"({memory['pixelPercent']}% of the {budgets['pixelBudget']:,} target; "
                f"hard limit {hard_px:,}; bitmap + {budgets['svgPixelWeight']}x SVG raster)",
                {'bitmapPx': memory['bitmapPx'], 'svgPx': int(memory['svgPx']),
                 'svgPixelWeight': budgets['svgPixelWeight'],
                 'deviceScale': budgets['deviceScale']}))
        elif memory['pixelCost'] > budgets['pixelBudget']:
            findings.append(_pf('error', 'pixel-over-target',
                f"project-wide costume pixels ~{memory['pixelCost']:,} "
                f"({memory['pixelPercent']}% of the {budgets['pixelBudget']:,} smooth target); "
                'above the smooth-play target, will stutter',
                {'bitmapPx': memory['bitmapPx'], 'svgRasterPx': memory['svgRasterPx'],
                 'svgPixelWeight': budgets['svgPixelWeight'],
                 'deviceScale': budgets['deviceScale']}))
        if memory['overDimension']:
            findings.append(_pf('warning', 'svg-over-dimension',
                                f"{len(memory['overDimension'])} SVG(s) exceed "
                                f"{budgets['svgMaxWidth']}x{budgets['svgMaxHeight']}",
                                memory['overDimension'][:10]))
        svg_bytes = memory['svgRasterPx'] * budgets['svgBytesPerPx']
        if svg_bytes > budgets['memoryBudget']:
            findings.append(_pf('warning', 'svg-hang-risk',
                                f"SVG raster alone is {svg_bytes / 2**30:.1f} GiB (budget "
                                f"{budgets['memoryBudget'] / 2**30:.2f} GiB); large SVG projects "
                                'can hang a low-RAM device, not just crash the tab (count is '
                                'irrelevant: 4096 4x4 skins are fine)',
                                {'svgSkins': memory['svgSkins'],
                                 'bitmapSkins': memory['bitmapSkins'],
                                 'svgRasterBytes': svg_bytes}))
        if memory['svgPx'] > budgets['svgStoredBudget']:
            findings.append(_pf('warning', 'slow-load-risk',
                                f"{memory['svgPx'] / 1e6:.0f}M stored SVG pixels; large SVG "
                                'projects can take minutes to load and may be discarded by '
                                'mobile tab management before they play',
                                {'svgPx': memory['svgPx'], 'svgSkins': memory['svgSkins']}))
        if memory['cssMathSkins']:
            findings.append(_pf('warning', 'svg-css-math',
                                f"{memory['cssMathSkins']} SVG skin(s) use CSS calc()/matrix3d()/"
                                'variables; browser CSS math can dominate startup even when '
                                'textures are small',
                                {'cssMathSkins': memory['cssMathSkins']}))
        if memory['svgElements'] > 50_000 or memory['maxSvgElements'] > 50_000:
            findings.append(_pf('warning', 'svg-complexity',
                                f"{memory['svgElements']:,} SVG elements across "
                                f"{memory['svgSkins']} skins (max {memory['maxSvgElements']:,}); "
                                'above ~50k elements, parsing/DOM can add tens of seconds to '
                                'load even when pixels are low (10k is instant, 100k is ~30s)',
                                {'svgElements': memory['svgElements'],
                                 'maxSvgElements': memory['maxSvgElements']}))
        if report['coverage']['unresolved']:
            findings.append(_pf('warning', 'unresolved-assets',
                                f"{report['coverage']['unresolved']} asset(s) had no measurable "
                                'dimensions (estimate is partial)'))
        if report['sounds']['decodedBytes'] > 100_000_000:
            findings.append(_pf('warning', 'sounds-decoded-large',
                                f"decoded sound PCM ~{report['sounds']['decodedBytes']:,} bytes "
                                f"({report['sounds']['unresolved']} unmeasured)"))
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        findings.append(_pf('error', 'invalid-archive', f'cannot read archive: {error}'))
    return report


def print_preflight(report: dict) -> None:
    j, m, a, l = report['json'], report['memory'], report['assets'], report['lists']
    print(f"project.json: {j['bytes']:,} / {j['budget']:,} bytes"
          f" ({j.get('percent', 0.0)}%)" + ('  OVER' if j['over'] else ''))
    print(f"assets: {a['count']} unique, {a['totalBytes']:,} bytes total, "
          f"{len(a['over'])} over {report['budgets']['assetBudget']:,}")
    print(f"memory (estimated): ~{m['bytes'] / 2 ** 30:.2f} GiB ({m['percent']}% of {_budget_text(m['budget'])})")
    print(f"  = bitmap {m['bitmapTexturePx']:,} tex px x{m['bitmapBytesPerPx']}"
          f" + svg {m['svgRasterPx']:,} raster px x{m['svgBytesPerPx']}"
          f" (viewBox {int(m['svgPx']):,} px, scale {m['deviceScale']}x)"
          f" + {report['sounds']['decodedBytes']:,} B sound PCM")
    print(f"costume pixels: {m['pixelCost']:,} / {m['pixelBudget']:,}"
          f" ({m['pixelPercent']}%; bitmap + {m['svgPixelWeight']}x SVG viewBox)"
          + ('  OVER' if m['pixelCost'] > m['pixelBudget'] else ''))
    print(f"svg unresolved: {m['svgUnresolved']}")
    print(f"largest list: {l['max']:,} / {report['budgets']['listLimit']:,} items")
    s = report['sounds']
    print(f"sounds: {s['count']} ({s['bytes']:,} bytes, ~{s['decodedBytes']:,} PCM, "
          f"{s['unresolved']} unmeasured)")
    print(f"measurement gaps: {report['coverage']['unresolved']}")
    print(f"sha256: {report.get('sha256', '')[:16]}...")
    for item in report['memory']['largest'][:5]:
        print(f"  largest: {item['sprite']}::{item['costume']} {item['texturePx']:,} tex px"
              f" (encoded {item['encodedPx']:,}, {item['kind']})")
    for finding in report['findings']:
        print(f"[{finding['level']:7}] {finding['code']}: {finding['message']}")
        detail = finding.get('detail')
        if isinstance(detail, dict):
            for suggestion in detail.get('suggestions', []):
                print(f"            -> {suggestion}")


_PREFLIGHT_MAX_ENTRIES = 200_000
_PREFLIGHT_MAX_UNCOMPRESSED = 2 * 1024 ** 3
_PREFLIGHT_MAX_SVG_READ = 32 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


_MP3_BITRATES = {
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _wav_pcm_bytes(data: bytes):
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    i = 12
    while i + 8 <= len(data):
        chunk = data[i:i + 4]
        size = struct.unpack('<I', data[i + 4:i + 8])[0]
        body = i + 8
        if chunk == b'data':
            return size
        i = body + size + (size & 1)
    return None


def _mp3_pcm_bytes(data: bytes, file_bytes: int):
    for i in range(min(len(data) - 4, 8192)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            version_bits = (data[i + 1] >> 3) & 0x03
            bitrate_index = (data[i + 2] >> 4) & 0x0F
            rate_index = (data[i + 2] >> 2) & 0x03
            if version_bits == 1 or bitrate_index in (0, 15) or rate_index == 3:
                continue
            table = _MP3_BITRATES[1 if version_bits == 3 else 2]
            rates = _MP3_RATES.get(version_bits)
            if not rates:
                continue
            bitrate = table[bitrate_index] * 1000
            rate = rates[rate_index]
            channels = 1 if ((data[i + 3] >> 6) & 0x03) == 3 else 2
            if bitrate <= 0:
                continue
            seconds = file_bytes * 8 / bitrate
            return int(seconds * rate * channels * 2)
    return None


def _sound_pcm_bytes(data: bytes, fmt: str, file_bytes: int):
    if fmt == 'wav':
        return _wav_pcm_bytes(data)
    if fmt == 'mp3':
        return _mp3_pcm_bytes(data, file_bytes)
    return None
