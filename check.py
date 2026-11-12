#!/usr/bin/env python3
# Kavkaz IPTV: night check of all channels from PLAYLIST_URL.
# Only checks that the channel's stream answers (the first bytes of the playlist/stream).
# Stream links are NOT saved: check.json has only channel names, groups and the error.
import os, re, json, time, urllib.request, concurrent.futures as cf

UA = 'VLC/3.0.20 LibVLC/3.0.20'
OUT = os.environ.get('OUT', 'prep')

def get(url, n=2048, timeout=12, rng=True):
    h = {'User-Agent': UA}
    if rng:
        h['Range'] = 'bytes=0-%d' % (n - 1)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(n)

def read_playlist(url):
    st, data = get(url, 50 * 1024 * 1024, 120, False)
    text = data.decode('utf-8', 'replace')
    chans, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF'):
            name = line.rsplit(',', 1)[-1].strip()
            g = re.search(r'group-title="([^"]*)"', line)
            cur = {'name': name, 'group': g.group(1) if g else ''}
        elif line.startswith('#EXTGRP:') and cur:
            cur['group'] = cur['group'] or line[8:].strip()
        elif line and not line.startswith('#') and cur:
            cur['url'] = line
            chans.append(cur)
            cur = None
    return chans

def check(ch):
    err = ''
    for attempt in range(2):
        try:
            st, b = get(ch['url'])
            if b.lstrip().startswith(b'#EXTM3U') or (b and b[0] == 0x47) or b[4:8] == b'ftyp':
                return None
            err = 'пустой ответ' if not b else 'не видео'
        except urllib.error.HTTPError as e:
            err = 'HTTP %d' % e.code
        except Exception as e:
            err = 'нет ответа' if 'timed out' in str(e) else type(e).__name__
        time.sleep(3)
    return {'name': ch['name'], 'group': ch['group'], 'e': err}

def main():
    pl = os.environ.get('PLAYLIST_URL', '').strip()
    if not pl:
        raise SystemExit('PLAYLIST_URL is empty')
    chans = read_playlist(pl)
    # 2 at a time, so the provider does not see many connections from one account
    with cf.ThreadPoolExecutor(2) as ex:
        res = list(ex.map(check, chans))
    bad = [r for r in res if r]
    os.makedirs(OUT, exist_ok=True)
    out = {'at': int(time.time() * 1000), 'total': len(chans), 'bad': bad}
    with open(os.path.join(OUT, 'check.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('channels: %d, not working: %d' % (len(chans), len(bad)))

if __name__ == '__main__':
    main()
