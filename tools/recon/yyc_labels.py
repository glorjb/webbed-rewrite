"""Build a Ghidra label map for a 32-bit GameMaker 2.3 YYC executable.

Recovers (all by static pattern matching, no execution):
  gml_*         compiled GML events/scripts      {name*, func*, var*} records in .data
  F_<builtin>   runner implementation of a built-in   push 1; push argc; push impl; push "name"; call Function_Add
  GV_<var>_get / _set   built-in variable getter/setter  push 1; push set; push get; push "name"; call
  g_FUNC_<name> runtime-filled built-in pointer slot    {name*, 0} in .data
  g_VAR_<name>  instance/global variable slot id         {name*, -1} in .data

Usage: python yyc_labels.py webbed.exe labels.json
"""
import json
import re
import struct
import sys

d = open(sys.argv[1], 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
base = struct.unpack_from('<I', d, pe + 52)[0]
so = pe + 24 + struct.unpack_from('<H', d, pe + 20)[0]
secs = []
for i in range(struct.unpack_from('<H', d, pe + 6)[0]):
    o = so + 40 * i
    name = d[o:o + 8].rstrip(b'\0').decode()
    vsz, va, rsz, raw = struct.unpack_from('<4I', d, o + 8)
    secs.append((name, base + va, vsz, raw, rsz))


def off2va(off):
    for n, va, vsz, raw, rsz in secs:
        if raw <= off < raw + rsz:
            return va + off - raw


def va2off(v):
    for n, va, vsz, raw, rsz in secs:
        if va <= v < va + rsz:
            return raw + v - va


def sec_of(v):
    for n, va, vsz, raw, rsz in secs:
        if va <= v < va + vsz:
            return n


IDENT = re.compile(rb'[A-Za-z_][A-Za-z0-9_@$]{1,80}\x00')


def cstr(v):
    o = va2off(v)
    if o is None:
        return None
    m = IDENT.match(d, o)
    if m and (o == 0 or d[o - 1] in (0,) or True):
        return m.group()[:-1].decode()


labels = {}      # va -> (label, kind)
builtin_impl = {}
builtin_argc = {}

# 1. gml_* records
for n, va, vsz, raw, rsz in secs:
    if n != '.data':
        continue
    for o in range(raw, raw + rsz - 11, 4):
        p, f, var = struct.unpack_from('<3I', d, o)
        if sec_of(p) == '.rdata' and sec_of(f) == '.text':
            s = cstr(p)
            if s and s.startswith('gml_'):
                labels.setdefault(f, (s, 'gml'))
                labels.setdefault(off2va(o), ('rec_' + s, 'data'))

# 2. Function_Add / variable registration in .text
text = next(s for s in secs if s[0] == '.text')
t0, t1 = text[3], text[3] + text[4]
for m in re.finditer(rb'\x6a[\x00\x01]\x6a(.)\x68(....)\x68(....)\xe8', d[t0:t1], re.S):
    argc = m.group(1)[0]
    impl, namep = struct.unpack('<2I', m.group(2) + m.group(3))
    s = cstr(namep)
    if s and sec_of(impl) == '.text':
        builtin_impl[s] = impl
        builtin_argc[s] = argc if argc < 0x80 else argc - 256
        labels.setdefault(impl, ('F_' + s, 'builtin'))
for m in re.finditer(rb'\x6a[\x00\x01]\x68(....)\x68(....)\x68(....)\xe8', d[t0:t1], re.S):
    setter, getter, namep = struct.unpack('<3I', m.group(1) + m.group(2) + m.group(3))
    s = cstr(namep)
    if s and sec_of(getter) == '.text':
        labels.setdefault(getter, ('GV_' + s + '_get', 'builtinvar'))
        if sec_of(setter) == '.text':
            labels.setdefault(setter, ('GV_' + s + '_set', 'builtinvar'))

# 3. g_FUNC_ / g_VAR_ slots: {name*, 0 | -1} pairs in .data
for n, va, vsz, raw, rsz in secs:
    if n != '.data':
        continue
    for o in range(raw, raw + rsz - 7, 4):
        p, v = struct.unpack_from('<2I', d, o)
        if sec_of(p) != '.rdata':
            continue
        if v not in (0, 0xFFFFFFFF):
            continue
        s = cstr(p)
        if not s:
            continue
        a = off2va(o)
        if a in labels:
            continue
        if v == 0 and (s in builtin_impl or s.startswith('gml_Script_')):
            labels[a] = ('g_FUNC_' + s, 'funcslot')
        elif v == 0xFFFFFFFF:
            labels[a] = ('g_VAR_' + s, 'varslot')

out = {
    'image_base': hex(base),
    'labels': {hex(k): v for k, v in sorted(labels.items())},
    'builtins': {k: {'impl': hex(v), 'argc': builtin_argc[k]} for k, v in sorted(builtin_impl.items())},
}
json.dump(out, open(sys.argv[2], 'w'), indent=0)
kinds = {}
for _, (_, k) in labels.items():
    kinds[k] = kinds.get(k, 0) + 1
print('labels by kind:', kinds, ' builtins:', len(builtin_impl))
