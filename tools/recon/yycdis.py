"""Annotated x86 disassembly of a GameMaker 2.3 YYC function.

Annotations:  <label> from labels.json, {var NAME} for YYC variable ids (>=100000),
              "string" for C strings, =VALUE for constant RValues (double, kind REAL).
Usage: python yycdis.py <webbed.exe> <labels.json> <vars.json> <func-name-or-0xaddr> [max_insns]
Requires: capstone
"""
import json
import struct
import sys

import capstone

exe, labels_path, vars_path, target = sys.argv[1:5]
max_insns = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5].isdigit() else 60000
d = open(exe, 'rb').read()
lab = {int(k, 16): v[0] for k, v in json.load(open(labels_path))['labels'].items()}
by_name = {v: k for k, v in lab.items()}
var_name = {v: k for k, v in json.load(open(vars_path)).items()}

pe = struct.unpack_from('<I', d, 0x3c)[0]
base = struct.unpack_from('<I', d, pe + 52)[0]
so = pe + 24 + struct.unpack_from('<H', d, pe + 20)[0]
secs = []
for i in range(struct.unpack_from('<H', d, pe + 6)[0]):
    o = so + 40 * i
    vsz, va, rsz, raw = struct.unpack_from('<4I', d, o + 8)
    secs.append((d[o:o + 8].rstrip(b'\0').decode(), base + va, rsz, raw))
data_lo = min(va for n, va, _, _ in secs if n == '.rdata')
data_hi = max(va + rsz for n, va, rsz, _ in secs if n in ('.data', '.mydata'))
# .data virtual size exceeds raw size: the tail is zero-filled BSS
bss_lo, bss_hi = [(va + rsz, va + vsz) for n, va, rsz, _ in secs for vsz in [
    struct.unpack_from('<I', d, so + 40 * [s[0] for s in secs].index('.data') + 8)[0]] if n == '.data'][0]
lift = '--lift' in sys.argv


def rd(v, n):
    for _, va, rsz, raw in secs:
        if va <= v < va + rsz:
            o = raw + v - va
            return d[o:o + n]
    return b''


def cstr(v):
    s = rd(v, 120).split(b'\0')[0]
    if len(s) > 1 and all(32 <= c < 127 for c in s):
        return s.decode()


def annotate(v):
    if v >= 100000 and v in var_name:
        return '{var %s}' % var_name[v]
    if v in lab:
        return '<%s>' % lab[v]
    if v - 4 in lab:
        return '<%s+4>' % lab[v - 4]
    s = cstr(v)
    if s and len(s) > 2:
        return '"%s"' % s[:60]
    if bss_lo <= v < bss_hi:
        return '=0(bss)'
    if data_lo <= v < data_hi:
        b = rd(v, 16)
        if len(b) == 16:
            dv = struct.unpack('<d', b[:8])[0]
            kind = struct.unpack('<I', b[12:16])[0]
            if kind == 0 and (dv == 0 or 1e-6 < abs(dv) < 1e9):
                return '=%r' % dv
    return ''


start = int(target, 16) if target.startswith('0x') else by_name[target]
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = False
code = rd(start, max_insns * 10)
stmts, cur = [], None
end = min([a for a in lab if a > start and lab[a].startswith(('gml_', 'F_', 'GV_'))] + [start + max_insns * 10])
line_slot = None
for i, ins in enumerate(md.disasm(code, start)):
    if i >= max_insns or ins.address >= end:
        break
    # YYC stack-trace frame: mov [esi+X], "gml_<name>" ; line number lives at [esi+X+4]
    if line_slot is None and ins.mnemonic == 'mov' and ins.op_str.startswith('dword ptr [esi + ') and '], 0x' in ins.op_str:
        val = int(ins.op_str.split('], ')[1], 16)
        if (cstr(val) or '').startswith('gml_'):
            line_slot = 'dword ptr [esi + 0x%x], ' % (int(ins.op_str.split('+ ')[1].split(']')[0], 16) + 4)
    notes = []
    for tok in ins.op_str.replace('[', ' ').replace(']', ' ').replace(',', ' ').replace('+', ' ').split():
        if tok.startswith('0x'):
            a = annotate(int(tok, 16))
            if a:
                notes.append(a)
    if lift:
        # YYC stores the GML source line before each statement: mov dword ptr [esi + 0x20], N
        if line_slot and ins.mnemonic == 'mov' and ins.op_str.startswith(line_slot) and ins.op_str.split(', ')[1].startswith('0x') or (line_slot and ins.mnemonic == 'mov' and ins.op_str.startswith(line_slot) and ins.op_str.split(', ')[1].isdigit()):
            n = int(ins.op_str.split(', ')[1], 0)
            if cur and n != cur[0]:
                stmts.append(cur)
                cur = None
            cur = cur or [n, []]
        elif cur is not None and notes and not (ins.mnemonic == 'mov' and 'fs:' in ins.op_str):
            cur[1].extend(x.replace('+4>', '>') for x in notes)
        elif cur is not None and ins.mnemonic == 'call' and ins.op_str.startswith('0x'):
            t = int(ins.op_str, 16)
            if t in lab and lab[t].startswith(('gml_', 'F_')):
                cur[1].append('CALL<%s>' % lab[t])
    else:
        print('%08x  %-6s %s  %s' % (ins.address, ins.mnemonic, ins.op_str, ' '.join(notes)))

if lift:
    if cur:
        stmts.append(cur)
    seen = {}
    for n, items in stmts:
        seen.setdefault(n, []).extend(items)
    for n in sorted(seen):
        items = [x for x in seen[n] if x]
        if items:
            print('L%-4d %s' % (n, ' '.join(items)))
