"""Offline preflight scan: Scratch compatibility, size and memory, no browser.

Extracted from gsdev.py so the scan can be exercised without the CLI. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


# --- Preflight: Scratch compatibility + memory (offline, no browser) ----------

PREFLIGHT_SCHEMA = 1
_PREFLIGHT_DEFAULTS = {
    "jsonBudget": 5 * 1024 * 1024,      # Scratch's "5 MB" = 5 MiB
    "assetBudget": 10 * 1024 * 1024,    # Scratch's "10 MB" = 10 MiB
    "listLimit": 200_000,
    "memoryBudget": 512 * 1024 ** 2,    # mobile-safe ceiling: iOS/Android OOM well below
                                        # the ~2 GiB desktop browser limit (raise for desktop)
    "svgMemoryFactor": 4,               # SVG needs 4x the pixels (then *4 bytes)
    "svgMaxWidth": 2400,
    "svgMaxHeight": 1800,
}


def _memory_suggestions(report: dict, budgets: dict) -> list[str]:
    memory = report['memory']
    suggestions = []
    if memory['svgPx'] > 0:
        suggestions.append(
            'convert SVG costumes to bitmap (primary fix:'
            f" ~{budgets['svgMemoryFactor']}x fewer pixels than SVG, no per-costume canvas)")
    suggestions.append('pack frames into a spritesheet with fewer, smaller frames (keeps crisp SVG)')
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

    width, height = scalar(root.attrib.get("width")), scalar(root.attrib.get("height"))
    if width and height:
        return {"status": "px", "width": width, "height": height}
    view_box = root.attrib.get("viewBox")
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return {"status": "viewbox", "width": abs(float(parts[2])), "height": abs(float(parts[3]))}
            except ValueError:
                pass
    return {"status": "unresolved", "reason": "dim"}


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
            'bitmapPx': 0, 'svgPx': 0, 'svgUnresolved': 0,
            'svgMemoryFactor': budgets['svgMemoryFactor'], 'effectivePx': 0,
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
                            if isinstance(dims, dict):
                                if dims.get('status') == 'unresolved':
                                    info['unresolved'] = True
                                else:
                                    info['dims'] = (dims['width'], dims['height'])
                            elif dims:
                                info['dims'] = dims
                            else:
                                info['unresolved'] = True
                        cache[md5ext] = info
                    if info['unresolved'] or not info['dims']:
                        report['coverage']['unresolved'] += 1
                        if fmt == 'svg':
                            report['memory']['svgUnresolved'] += 1
                        continue
                    width, height = info['dims']
                    is_svg = info['kind'] == 'svg'
                    pixels = width * height
                    if not is_svg:
                        # Scratch's texture is the stored bitmap at
                        # bitmapResolution (2x saves -> quarter the pixels).
                        res = costume.get('bitmapResolution', 1) or 1
                        pixels = pixels / (res * res)
                    pixels = int(round(pixels))
                    if is_svg:
                        report['memory']['svgPx'] += pixels
                        if width > budgets['svgMaxWidth'] or height > budgets['svgMaxHeight']:
                            report['memory']['overDimension'].append(
                                {'sprite': target.get('name'), 'costume': costume.get('name'),
                                 'width': width, 'height': height})
                    else:
                        report['memory']['bitmapPx'] += pixels
                    costume_px.append((pixels, target.get('name'), costume.get('name'), info['kind']))

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
            {'pixels': p, 'sprite': s, 'costume': c, 'kind': k}
            for p, s, c, k in sorted(costume_px, reverse=True)[:10]
        ]
        memory = report['memory']
        memory['effectivePx'] = int(memory['bitmapPx'] + budgets['svgMemoryFactor'] * memory['svgPx'])
        memory['bytes'] = memory['effectivePx'] * 4 + report['sounds']['decodedBytes']
        memory['percent'] = round(memory['bytes'] / budgets['memoryBudget'] * 100, 1) if budgets['memoryBudget'] else 0.0
        if memory['bytes'] > budgets['memoryBudget']:
            findings.append(_pf('error', 'memory-over-budget',
                f"worst-case memory (all costumes loaded) ~{memory['bytes'] / 2 ** 30:.2f} GiB "
                f"({memory['percent']}% of {_budget_text(budgets['memoryBudget'])})",
                {'bitmapPx': memory['bitmapPx'], 'svgPx': int(memory['svgPx']),
                 'svgFactor': budgets['svgMemoryFactor'],
                 'soundPcmBytes': report['sounds']['decodedBytes'],
                 'suggestions': _memory_suggestions(report, budgets)}))
        if memory['overDimension']:
            findings.append(_pf('warning', 'svg-over-dimension',
                                f"{len(memory['overDimension'])} SVG(s) exceed "
                                f"{budgets['svgMaxWidth']}x{budgets['svgMaxHeight']}",
                                memory['overDimension'][:10]))
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
    print(f"memory (worst case): ~{m['bytes'] / 2 ** 30:.2f} GiB ({m['percent']}% of {_budget_text(m['budget'])})"
          f"  = (bitmap {m['bitmapPx']:,} px + svg {int(m['svgPx']):,} px x{m['svgMemoryFactor']}) x4"
          f" + {report['sounds']['decodedBytes']:,} B sound PCM")
    print(f"svg unresolved: {m['svgUnresolved']}")
    print(f"largest list: {l['max']:,} / {report['budgets']['listLimit']:,} items")
    s = report['sounds']
    print(f"sounds: {s['count']} ({s['bytes']:,} bytes, ~{s['decodedBytes']:,} PCM, "
          f"{s['unresolved']} unmeasured)")
    print(f"measurement gaps: {report['coverage']['unresolved']}")
    print(f"sha256: {report.get('sha256', '')[:16]}...")
    for item in report['memory']['largest'][:5]:
        print(f"  largest: {item['sprite']}::{item['costume']} {item['pixels']:,} px ({item['kind']})")
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
