"""Recover YYC {name*, func*, var*} records from a 32-bit GameMaker YYC exe.
Usage: python yyc_functable.py webbed.exe out.json"""
import struct, sys, re, json, collections
d = open(sys.argv[1], 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
nsec = struct.unpack_from('<H', d, pe + 6)[0]
opt = pe + 24
base = struct.unpack_from('<I', d, opt + 28)[0]
secs = []
so = opt + struct.unpack_from('<H', d, pe + 20)[0]
for i in range(nsec):
    o = so + 40 * i
    name = d[o:o+8].rstrip(b'\0').decode()
    vsz, va, rsz, raw = struct.unpack_from('<4I', d, o + 8)
    secs.append((name, base + va, vsz, raw, rsz))
def off2va(off):
    for n, va, vsz, raw, rsz in secs:
        if raw <= off < raw + rsz: return va + off - raw
def va2off(v):
    for n, va, vsz, raw, rsz in secs:
        if va <= v < va + rsz: return raw + v - va
def sec_of(v):
    for n, va, vsz, raw, rsz in secs:
        if va <= v < va + vsz: return n
names = {}
for m in re.finditer(rb'gml_[A-Za-z0-9_]+\x00', d):
    names[off2va(m.start())] = m.group()[:-1].decode()
# index every aligned dword in data sections
ptr_at = collections.defaultdict(list)
for n, va, vsz, raw, rsz in secs:
    if n in ('.text',): continue
    for o in range(raw, raw + rsz - 3, 4):
        v = struct.unpack_from('<I', d, o)[0]
        if v in names: ptr_at[v].append(off2va(o))
out = {}
shapes = collections.Counter()
for sva, name in names.items():
    for rva in ptr_at.get(sva, []):
        o = va2off(rva)
        f, var = struct.unpack_from('<2I', d, o + 4)
        if sec_of(f) == '.text':
            out.setdefault(name, {'func': hex(f), 'record': hex(rva), 'var': hex(var) if var else None})
            shapes['ok'] += 1
print('sections:', [(s[0], hex(s[1])) for s in secs])
print('gml strings:', len(names), 'resolved functions:', len(out), shapes)
json.dump(dict(sorted(out.items())), open(sys.argv[2], 'w'), indent=1)
