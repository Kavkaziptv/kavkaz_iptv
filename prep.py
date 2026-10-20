#!/usr/bin/env python3
"""
Prepares the heavy things once, on GitHub, so TV boxes only download small ready files:
  out/guide.bin        the programme guide in the app's own format (EpgStore.java) — no XML to read on the box
  out/logos/<sha1>.png every channel logo made small (<= 112 px); <sha1> = SHA-1 of the logo's address
  out/meta.json        when it was made and how much is inside
  out/api/now.json     what is on now and in the next hours, all channels (titles only) — Cloudflare /epg/now
  out/api/s-NNN.json   all days of each channel, split into 128 parts by channel — Cloudflare /epg/ch?id=…

Addresses come from the environment (GitHub → Settings → Secrets → Actions), never from this file:
  PLAYLIST_URL  the playlist (only channel ids, names and logo addresses are read; stream links are NOT saved)
  EPG_URL       the guide (optional: taken from the playlist's url-tvg when missing; several separated by spaces)
"""
import concurrent.futures as cf
import gzip
import hashlib
import io
import json
import os
import re
import struct
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

OUT = os.environ.get('OUT', 'out')
UA = 'KavkazIPTV-prep'
NOW = int(time.time() * 1000)
KEEP_MS = int(float(os.environ.get('KEEP_DAYS', '4')) * 86400000)
AHEAD_MS = int(float(os.environ.get('AHEAD_DAYS', '2.5')) * 86400000)
DESC_LEN = 180
LOGO = 160


def get(url, timeout=120):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    while data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data


# ---------------------------------------------------------------- playlist (ids, names, logos only)
def read_playlist(url):
    text = get(url).decode('utf-8', 'replace')
    head = text[:8000]
    tvg = re.search(r'(?:x-tvg-url|url-tvg|tvg-url)\s*=\s*"([^"]+)"', head) or re.search(r"(?:x-tvg-url|url-tvg|tvg-url)\s*=\s*'([^']+)'", head)
    chans = []
    for line in text.splitlines():
        if not line.startswith('#EXTINF'):
            continue
        attr = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        name = line.rsplit(',', 1)[-1].strip()
        chans.append({'id': attr.get('tvg-id', ''), 'name': (attr.get('tvg-name') or name).strip().lower(), 'logo': attr.get('tvg-logo', '')})
    epg = [u.strip() for u in re.split(r'[,\s]+', tvg.group(1)) if u.strip()] if tvg else []
    print('playlist: channels', len(chans), '· guide addresses in it:', len(epg))
    return chans, epg


# ---------------------------------------------------------------- guide
def parse_time(s):
    s = (s or '').strip()
    if len(s) < 12:
        return 0
    try:
        y, mo, d, h, mi = int(s[0:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]), int(s[10:12])
        sec = int(s[12:14]) if len(s) >= 14 and s[12:14].isdigit() else 0
        import calendar
        t = calendar.timegm((y, mo, d, h, mi, sec)) * 1000
        m = re.search(r'([+-])(\d{2})(\d{2})', s[14:] if len(s) > 14 else '')
        if m:
            off = (int(m.group(2)) * 60 + int(m.group(3))) * 60000
            t -= off if m.group(1) == '+' else -off
        return t
    except Exception:
        return 0


def read_guide(urls, want_ids, want_names):
    by, icons, names = {}, {}, {}
    keep = set(want_ids)
    frm, to = NOW - KEEP_MS, NOW + AHEAD_MS
    for url in urls:
        try:
            data = get(url, 600)
        except Exception as e:
            print('guide failed:', url.split('?')[0], e, file=sys.stderr)
            continue
        for ev, el in ET.iterparse(io.BytesIO(data), events=('end',)):
            if el.tag == 'channel':
                cid = el.get('id', '')
                ic = el.find('icon')
                if ic is not None and ic.get('src') and cid not in icons:
                    icons[cid] = ic.get('src')
                for dn in el.findall('display-name'):
                    n = (dn.text or '').strip().lower()
                    if n and n not in names:
                        names[n] = cid
                    if n in want_names or not want_names:
                        keep.add(cid)
                el.clear()
            elif el.tag == 'programme':
                cid = el.get('channel', '')
                if want_ids or want_names:
                    if cid not in keep:
                        el.clear(); continue
                s, e = parse_time(el.get('start')), parse_time(el.get('stop'))
                if s and e and e >= frm and s <= to:
                    t = (el.findtext('title') or '').strip()[:140]
                    d = ''
                    if s < NOW + 86400000 and e > NOW - 6 * 3600000:
                        d = (el.findtext('desc') or '').strip()[:DESC_LEN]
                    by.setdefault(cid, []).append((s, e, t, d))
                el.clear()
    for l in by.values():
        l.sort(key=lambda p: p[0])
    return by, icons, names


def mutf8(s):
    """Java DataOutputStream.writeUTF: 2-byte length + modified UTF-8."""
    s = s[:2000]
    b = bytearray()
    for ch in s:
        o = ord(ch)
        units = [o] if o < 0x10000 else [0xD800 + ((o - 0x10000) >> 10), 0xDC00 + ((o - 0x10000) & 0x3FF)]
        for u in units:
            if 0 < u < 0x80:
                b.append(u)
            elif u < 0x800:
                b += bytes((0xC0 | (u >> 6), 0x80 | (u & 0x3F)))
            else:
                b += bytes((0xE0 | (u >> 12), 0x80 | ((u >> 6) & 0x3F), 0x80 | (u & 0x3F)))
    if len(b) > 65535:
        return mutf8(s[: len(s) // 2])
    return struct.pack('>H', len(b)) + bytes(b)


def write_guide(path, by, icons, names):
    o = io.BytesIO()
    o.write(struct.pack('>i', 0x4B47)); o.write(mutf8('prep')); o.write(struct.pack('>q', NOW))
    o.write(struct.pack('>i', len(icons)))
    for k, v in icons.items():
        o.write(mutf8(k)); o.write(mutf8(v))
    o.write(struct.pack('>i', len(names)))
    for k, v in names.items():
        o.write(mutf8(k)); o.write(mutf8(v))
    o.write(struct.pack('>i', len(by)))
    for cid, l in by.items():
        o.write(mutf8(cid)); o.write(struct.pack('>i', len(l)))
        for s, e, t, d in l:
            o.write(struct.pack('>qq', s, e)); o.write(mutf8(t)); o.write(mutf8(d))
    with open(path + '.tmp', 'wb') as f:
        f.write(gzip.compress(o.getvalue(), 9))
    os.replace(path + '.tmp', path)


# ---------------------------------------------------------------- logos
def logo_one(url):
    from PIL import Image
    key = hashlib.sha1(url.encode('utf-8')).hexdigest()
    path = os.path.join(OUT, 'logos', key + '.png')
    if os.path.exists(path):
        return 'have'
    for u in (url, 'http:' + url[6:] if url.startswith('https:') else None):
        if not u:
            continue
        try:
            data = get(u, 20)
            im = Image.open(io.BytesIO(data))
            im.load()
            im = im.convert('RGBA')
            im.thumbnail((LOGO, LOGO), Image.LANCZOS)
            im.save(path, 'PNG', optimize=True)
            return 'new'
        except Exception:
            pass
    return 'bad'


SHARDS = 128


def shard_of(gid):
    return int(hashlib.sha1(gid.encode('utf-8')).hexdigest()[:4], 16) % SHARDS


def write_api(by, icons, names, want_names):
    """Small files the Cloudflare worker hands out, so TVs never download the whole guide."""
    d = os.path.join(OUT, 'api')
    os.makedirs(d, exist_ok=True)
    frm, to = NOW - 2 * 3600000, NOW + 10 * 3600000
    now = {}
    for gid, l in by.items():
        part = [[s, e, t] for s, e, t, _ in l if e > frm and s < to]
        if part:
            now[gid] = part
    n = {k: v for k, v in names.items() if v in by and (not want_names or k in want_names)}
    i = {}
    for gid, u in icons.items():
        if gid in by and u.startswith('http'):
            h = hashlib.sha1(u.encode('utf-8')).hexdigest()
            if os.path.exists(os.path.join(OUT, 'logos', h + '.png')):
                i[gid] = h
    with open(os.path.join(d, 'now.json'), 'w', encoding='utf-8') as f:
        json.dump({'at': NOW, 'ids': now, 'n': n, 'i': i, 'shards': SHARDS}, f, ensure_ascii=False, separators=(',', ':'))
    parts = [dict() for _ in range(SHARDS)]
    for gid, l in by.items():
        parts[shard_of(gid)][gid] = [[s, e, t, dd] if dd else [s, e, t] for s, e, t, dd in l]
    for k, part in enumerate(parts):
        with open(os.path.join(d, 's-%03d.json' % k), 'w', encoding='utf-8') as f:
            json.dump(part, f, ensure_ascii=False, separators=(',', ':'))
    print('api: now.json', os.path.getsize(os.path.join(d, 'now.json')) // 1024, 'KB,', SHARDS, 'parts')


def main():
    os.makedirs(os.path.join(OUT, 'logos'), exist_ok=True)
    pl = os.environ.get('PLAYLIST_URL', '').strip()
    chans, tvg = (read_playlist(pl) if pl else ([], []))
    epg = os.environ.get('EPG_URL', '').split() or tvg
    if not epg:
        sys.exit('no guide address: the playlist has no url-tvg / x-tvg-url — add the guide link as the EPG_URL secret')
    want_ids = {c['id'] for c in chans if c['id']}
    want_names = {c['name'] for c in chans if c['name']}
    t0 = time.time()
    by, icons, names = read_guide(epg, want_ids, want_names)
    if by:
        write_guide(os.path.join(OUT, 'guide.bin'), by, icons, names)
    progs = sum(len(l) for l in by.values())
    print('guide: channels', len(by), 'programmes', progs, 'in', round(time.time() - t0), 's')

    urls = sorted({c['logo'] for c in chans if c['logo'].startswith('http')} | {u for u in icons.values() if u.startswith('http')})
    stat = {}
    with cf.ThreadPoolExecutor(16) as ex:
        for r in ex.map(logo_one, urls):
            stat[r] = stat.get(r, 0) + 1
    print('logos:', stat)
    if by:
        write_api(by, icons, names, want_names)
    meta = {'at': NOW, 'channels': len(by), 'programmes': progs, 'logos': len(os.listdir(os.path.join(OUT, 'logos')))}
    with open(os.path.join(OUT, 'meta.json'), 'w') as f:
        json.dump(meta, f)
    if not by:
        sys.exit('the guide could not be read')


if __name__ == '__main__':
    main()
