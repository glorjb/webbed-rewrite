"""Symbolic lifter for GameMaker 2.3 YYC (x86) event/script functions -> pseudo-GML trace.

It does NOT reconstruct control flow. It linearly walks the function and tracks:
  * RValue temporaries in the frame ([esi+X]), constant RValues, variable references
  * argument arrays built for built-in calls (call helper: self, other, &ret, argc, fnptr, argv)
  * direct gml_Script_* calls, variable reads, arithmetic operator helpers, truthiness tests
and prints one line per side effect, grouped under the original GML source line number.
Branch instructions are printed as `?jcc -> target` so control flow can be followed by hand.

Every value printed comes from the binary; unresolved things are shown as `?<what>`.
Usage: python yyclift.py <exe> <labels.json> <vars.json> <asset_index.json> <function>
"""
import json
import struct
import sys

import capstone

exe, labels_path, vars_path, assets_path, target = sys.argv[1:6]
d = open(exe, 'rb').read()
lab = {int(k, 16): v[0] for k, v in json.load(open(labels_path))['labels'].items()}
by_name = {v: k for k, v in lab.items()}
var_name = {v: k for k, v in json.load(open(vars_path)).items()}
assets = json.load(open(assets_path))

pe = struct.unpack_from('<I', d, 0x3c)[0]
base = struct.unpack_from('<I', d, pe + 52)[0]
so = pe + 24 + struct.unpack_from('<H', d, pe + 20)[0]
secs = []
for i in range(struct.unpack_from('<H', d, pe + 6)[0]):
    o = so + 40 * i
    vsz, va, rsz, raw = struct.unpack_from('<4I', d, o + 8)
    secs.append((d[o:o + 8].rstrip(b'\0').decode(), base + va, vsz, rsz, raw))
DATA = next(s for s in secs if s[0] == '.data')
BSS = (DATA[1] + DATA[3], DATA[1] + DATA[2])
RDATA = next(s for s in secs if s[0] == '.rdata')

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True


def rd(v, n):
    for _, va, vsz, rsz, raw in secs:
        if va <= v < va + rsz:
            o = raw + v - va
            return d[o:o + n]
    return b''


def cstr(v):
    s = rd(v, 200).split(b'\0')[0]
    if s and all(32 <= c < 127 or c in (9, 10, 13) for c in s):
        return s.decode()


def const_rvalue(addr):
    """Constant RValue in .rdata/.data (16 bytes: payload, flags, kind) or zero BSS."""
    if BSS[0] <= addr < BSS[1]:
        return '0'
    b = rd(addr, 16)
    if len(b) < 16:
        return '?const@%x' % addr
    kind = struct.unpack_from('<I', b, 12)[0] & 0xffffff
    if kind == 0:
        v = struct.unpack_from('<d', b, 0)[0]
        return repr(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)
    if kind == 1:   # string: payload is RefString* -> char* (runtime-built)
        return '?string@%x' % addr
    if kind == 5:
        return 'undefined'
    if kind == 13:
        return 'true' if struct.unpack_from('<d', b, 0)[0] else 'false'
    if kind in (7, 10):
        return str(struct.unpack_from('<q' if kind == 10 else '<i', b, 0)[0])
    return '?kind%d@%x' % (kind, addr)


# ---------- classify unknown helpers lazily ----------
helper_cache = {}
KNOWN = {
    0x402130: 'assign', 0x401ff0: 'assign', 0x402310: 'assign',
    0x119bb40: 'callbuiltin', 0x117b440: 'truthy', 0x119e4d0: 'varread',
    0x1184a20: 'trace_enter',
}


def classify(t):
    if t in KNOWN:
        return KNOWN[t]
    if t in helper_cache:
        return helper_cache[t]
    kind = 'call'
    ins = list(md.disasm(rd(t, 400), t))[:120]
    calls = [int(i.op_str, 16) for i in ins if i.mnemonic == 'call' and i.op_str.startswith('0x')]
    if len(calls) >= 2 and calls[0] == 0x402310:
        core = calls[1]
        cins = list(md.disasm(rd(core, 600), core))[:160]
        ops = [i.mnemonic for i in cins]
        if 'imul' in ops:
            kind = 'op*'
        elif 'idiv' in ops or 'div' in ops:
            kind = 'op/'
        elif 'divsd' in ops:
            kind = 'op/'
        elif 'mulsd' in ops:
            kind = 'op*'
        elif 'subsd' in ops or 'sub' in ops[:60] and 'addsd' not in ops:
            kind = 'op-'
        elif 'addsd' in ops or 'add' in ops:
            kind = 'op+'
        else:
            kind = 'op?%x' % core
    helper_cache[t] = kind
    return kind


def builtin_var_name(a):
    """YYC pushes [rec+4] (runtime slot id); rec = {name*, id}. Name pointer is at a-4."""
    q = rd(a - 4, 4)
    return cstr(struct.unpack('<I', q)[0]) if len(q) == 4 else None


def vname(v):
    if v >= 100000 and v in var_name:
        return var_name[v]
    return None


# ---------- lifter ----------
start = int(target, 16) if target.startswith('0x') else by_name[target]
end = min([a for a in lab if a > start and lab[a].startswith(('gml_', 'F_', 'GV_'))] + [start + 0x200000])

local_targets = None
argv_seen = set()
for pass_no in (0, 1):
    regs = {}
    xmm = {}
    espargs = None
    slots = {}          # frame offset -> expression string
    rawd = {}           # frame offset -> last immediate dword written
    vstore = {}
    ret_slots = set()
    ptrs = {}           # frame offset -> frame offset it points to (argv entries)
    pushes = []
    line_slot = None
    cur_line = 0
    out = []
    tmp_n = [0]


    def emit(s):
        out.append((cur_line, s))


    def val_of(x):
        if x is None:
            return '?'
        k = x[0]
        if k == 'addr':
            if x[1] in slots:
                return slots[x[1]]
            lo, hi, kd = rawd.get(x[1]), rawd.get(x[1] + 4), rawd.get(x[1] + 12)
            if lo is not None and hi is not None and kd in (0, None):
                v = struct.unpack('<d', struct.pack('<II', lo, hi))[0]
                return repr(int(v)) if v == int(v) else repr(v)
            return '?tmp%x' % x[1]
        if k == 'imm':
            return str(x[1])
        if k == 'ref':
            return x[1]
        if k == 'expr':
            return x[1]
        return '?' + str(x)


    def describe_push(op, ins):
        """Map a push operand to a symbolic value."""
        if op.type == capstone.x86.X86_OP_IMM:
            return ('imm', op.imm & 0xffffffff)
        if op.type == capstone.x86.X86_OP_REG:
            return regs.get(ins.reg_name(op.reg))
        if op.type == capstone.x86.X86_OP_MEM:
            m = op.mem
            if m.base == 0 and m.index == 0:
                a = m.disp & 0xffffffff
                if (a - 4) in lab and lab[a - 4].startswith('g_FUNC_'):
                    return ('fn', lab[a - 4][7:])
                return ('mem', a)
            rn = ins.reg_name(m.base)
            if rn == 'ebp' and m.disp == 8:
                return ('ref', 'self')
            if rn == 'ebp' and m.disp == 0xc:
                return ('ref', 'other')
            if rn == 'esi':
                if m.disp in ptrs:
                    pv = ptrs[m.disp]
                    return ('expr', pv[1]) if isinstance(pv, tuple) else ('addr', pv)
                return ('expr', slots.get(m.disp, '?[%x]' % m.disp))
        return None


    def const_or_val(x):
        if x and x[0] == 'imm':
            a = x[1]
            if RDATA[1] <= a < BSS[1]:
                return const_rvalue(a)
        return val_of(x)


    for ins in md.disasm(rd(start, end - start), start):
        if ins.address >= end:
            break
        mn, ops = ins.mnemonic, ins.operands
        if mn == 'sub' and ins.op_str.startswith('esp, '):
            espargs = {}
            continue
        if mn in ('mov', 'movsd') and len(ops) == 2 and ops[0].type == capstone.x86.X86_OP_MEM and ins.reg_name(ops[0].mem.base) == 'esp' and espargs is not None:
            src = ops[1]
            if src.type == capstone.x86.X86_OP_REG:
                rn = ins.reg_name(src.reg)
                espargs[ops[0].mem.disp] = regs.get(rn) if not rn.startswith('xmm') else ('expr', xmm.get(rn, '?xmm'))
            elif src.type == capstone.x86.X86_OP_IMM:
                espargs[ops[0].mem.disp] = ('imm', src.imm & 0xffffffff)
            continue
        if mn == 'movsd' and len(ops) == 2 and ops[0].type == capstone.x86.X86_OP_REG and ops[1].type == capstone.x86.X86_OP_MEM and ops[1].mem.base == 0:
            b = rd(ops[1].mem.disp & 0xffffffff, 8)
            xmm[ins.reg_name(ops[0].reg)] = repr(struct.unpack('<d', b)[0]) if len(b) == 8 else '?'
            continue
        # stack-trace frame -> line slot
        if mn == 'mov' and len(ops) == 2 and ops[0].type == capstone.x86.X86_OP_MEM and ops[1].type == capstone.x86.X86_OP_IMM \
                and ins.reg_name(ops[0].mem.base) == 'esi':
            disp, imm = ops[0].mem.disp, ops[1].imm & 0xffffffff
            if line_slot is None and (cstr(imm) or '').startswith('gml_'):
                line_slot = disp + 4
                continue
            if disp == line_slot:
                cur_line = imm
                continue
            rawd[disp] = imm
            if disp - 4 in rawd and (disp - 4) not in slots:
                pass
            continue
        # direct stores into a variable RValue returned by GetYYVarRef: [reg+0]=lo, [reg+4]=hi, [reg+0xc]=kind
        if mn in ('mov', 'movsd') and len(ops) == 2 and ops[0].type == capstone.x86.X86_OP_MEM and ops[0].mem.base                 and (regs.get(ins.reg_name(ops[0].mem.base)) or (None,))[0] == 'varref':
            rn = ins.reg_name(ops[0].mem.base)
            ref = regs[rn][1]
            st = vstore.setdefault(rn, {})
            src = ops[1]
            if src.type == capstone.x86.X86_OP_IMM:
                st[ops[0].mem.disp] = src.imm & 0xffffffff
            elif src.type == capstone.x86.X86_OP_REG and ins.reg_name(src.reg).startswith('xmm'):
                st['dbl'] = xmm.get(ins.reg_name(src.reg), '?xmm')
            elif src.type == capstone.x86.X86_OP_REG:
                st[ops[0].mem.disp] = ('reg', regs.get(ins.reg_name(src.reg)))
            ready = isinstance(st.get(0xc), int) and (('dbl' in st) or (st[0xc] & 0xffffff) == 5
                                                      or (isinstance(st.get(0), int) and isinstance(st.get(4), int)))
            if ready:
                kind = st[0xc] & 0xffffff
                if 'dbl' in st:
                    v = st['dbl']
                elif isinstance(st.get(0), int) and isinstance(st.get(4), int):
                    if kind == 0:
                        dv = struct.unpack('<d', struct.pack('<II', st[0], st[4]))[0]
                        v = repr(int(dv)) if dv == int(dv) else repr(dv)
                    elif kind == 13:
                        v = 'true' if (st[0] or st[4]) else 'false'
                    elif kind == 5:
                        v = 'undefined'
                    elif kind in (7, 10):
                        v = str(st[0])
                    else:
                        v = '?kind%d' % kind
                elif kind == 5:
                    v = 'undefined'
                else:
                    v = '?'
                emit('%s = %s' % (ref, v))
                vstore.pop(rn, None)
            continue
        if mn == 'lea' and ops[1].type == capstone.x86.X86_OP_MEM and ins.reg_name(ops[1].mem.base) == 'esi':
            regs[ins.reg_name(ops[0].reg)] = ('addr', ops[1].mem.disp)
            continue
        if mn == 'mov' and len(ops) == 2:
            dst, src = ops
            if dst.type == capstone.x86.X86_OP_REG:
                rn = ins.reg_name(dst.reg)
                if src.type == capstone.x86.X86_OP_MEM:
                    m = src.mem
                    bn = ins.reg_name(m.base) if m.base else None
                    if bn == 'ebp' and m.disp == 0x18:
                        regs[rn] = ('argv',)
                    elif bn and (regs.get(bn) or (None,))[0] == 'argv':
                        regs[rn] = ('expr', 'argument%d' % (m.disp // 4))
                    elif bn == 'ebp' and m.disp == 8:
                        regs[rn] = ('ref', 'self')
                    elif bn == 'ebp' and m.disp == 0xc:
                        regs[rn] = ('ref', 'other')
                    elif bn is None and (m.disp & 0xffffffff) == 0x166dc10:
                        regs[rn] = ('ref', 'global')
                    elif bn and (regs.get(bn) or (None,))[0] == 'ref' and m.disp == 0:
                        regs[rn] = ('vtbl', regs[bn][1])
                    elif bn == 'esi' and m.disp in ptrs:
                        pv = ptrs[m.disp]
                        regs[rn] = ('expr', pv[1]) if isinstance(pv, tuple) else ('addr', pv)
                    else:
                        regs[rn] = None
                elif src.type == capstone.x86.X86_OP_REG:
                    regs[rn] = regs.get(ins.reg_name(src.reg))
                else:
                    regs[rn] = ('imm', src.imm & 0xffffffff)
            elif dst.type == capstone.x86.X86_OP_MEM and ins.reg_name(dst.mem.base) == 'esi' and src.type == capstone.x86.X86_OP_REG:
                v = regs.get(ins.reg_name(src.reg))
                if v and v[0] == 'addr':
                    ptrs[dst.mem.disp] = v[1]
                    argv_seen.add(v[1])
                elif v and v[0] == 'varref':
                    slots[dst.mem.disp] = v[1]
                    ptrs[dst.mem.disp] = ('var', v[1])
            continue
        if mn == 'push':
            pushes.append(describe_push(ops[0], ins))
            continue
        if mn == 'add' and ins.op_str.startswith('esp'):
            pushes = []
            continue
        if mn.startswith('j') and mn != 'jmp':
            emit('?%s -> %s' % (mn, ins.op_str))
            continue
        if mn == 'call':
            op = ops[0]
            if op.type == capstone.x86.X86_OP_MEM and op.mem.disp in (4, 8) and op.mem.index == 0:
                # vtable GetYYVarRef(id): owner from ecx
                owner = regs.get('ecx')
                ownername = owner[1] if owner and owner[0] == 'ref' else '?inst'
                vid = pushes[-1][1] if pushes and pushes[-1] and pushes[-1][0] == 'imm' else None
                nm = vname(vid) if vid else None
                ref = (('' if ownername == 'self' else ownername + '.') + (nm or '?var%s' % vid))
                regs['eax'] = ('varref', ref)
                pushes = []
                continue
            if op.type != capstone.x86.X86_OP_IMM:
                pushes = []
                continue
            t = op.imm & 0xffffffff
            kind = classify(t)
            if t == 0x117cb20:
                regs['eax'] = pushes[-1] if pushes else None
            elif t == 0x4055d0:
                dest = regs.get('ecx')
                a = pushes[-1][1] if pushes and pushes[-1] and pushes[-1][0] in ('imm', 'mem') else None
                txt = cstr(a) if a else None
                if txt is None and a:
                    q = rd(a, 4)
                    txt = cstr(struct.unpack('<I', q)[0]) if len(q) == 4 else None
                if dest and dest[0] == 'addr':
                    slots[dest[1]] = '"%s"' % txt if txt is not None else '?str'
            elif t in (0x119ea60, 0x119de50, 0x119e2d0, 0x119d2f0):
                try:
                    inst, vid, idx, v = pushes[-1], pushes[-2], pushes[-3], pushes[-4]
                    if vid and vid[0] == 'imm':
                        nm = vname(vid[1]) or '?var%d' % vid[1]
                    elif vid and vid[0] == 'mem' and builtin_var_name(vid[1]):
                        nm = builtin_var_name(vid[1])
                    elif vid and vid[0] == 'mem' and (vid[1] - 4) in lab:
                        nm = lab[vid[1] - 4].replace('g_VAR_', '')
                    elif vid and vid[0] == 'mem' and vid[1] in lab:
                        nm = lab[vid[1]].replace('g_VAR_', '')
                    else:
                        nm = '?var'
                    lhs = '%s.%s' % (val_of(inst), nm)
                    if idx and idx[0] == 'imm' and idx[1] != 0x80000000:
                        lhs += '[%d]' % idx[1]
                    if t in (0x119e2d0, 0x119d2f0):
                        if v and v[0] == 'addr':
                            slots[v[1]] = lhs
                    else:
                        emit('%s = %s' % (lhs, val_of(v)))
                except (IndexError, TypeError):
                    emit('?varaccess')
            elif t == 0x11c59b0 and espargs:
                a = espargs.get(0); b = espargs.get(4)
                regs['eax'] = ('expr', 'compare(%s, %s)' % (val_of(a), val_of(b)))
                emit('compare(%s, %s)' % (val_of(a), val_of(b)))
            elif kind == 'trace_enter':
                pass
            elif kind == 'assign':
                dest = regs.get('ecx')
                src = pushes[-1] if pushes else None
                v = const_or_val(src)
                if dest and dest[0] == 'addr':
                    if not (v == 'undefined' and str(slots.get(dest[1], '')).startswith('argument')):
                        if local_targets is not None and dest[1] not in local_targets and dest[1] >= 0x40:
                            emit('local_%x = %s' % (dest[1], v))
                            slots[dest[1]] = 'local_%x' % dest[1]
                        else:
                            slots[dest[1]] = v
                    p = ptrs.get(dest[1])
                elif dest and dest[0] == 'varref':
                    emit('%s = %s' % (dest[1], v))
            elif kind == 'callbuiltin':
                # pushes (push order): argv, fn, argc, &ret, other, self
                try:
                    argv, fn, argc, ret = pushes[-6], pushes[-5], pushes[-4], pushes[-3]
                    n = argc[1] if argc and argc[0] == 'imm' else 0
                    args = []
                    for i in range(n):
                        p = ptrs.get(argv[1] + 4 * i) if argv and argv[0] == 'addr' else None
                        if isinstance(p, tuple) and p[0] == 'var':
                            args.append(p[1])
                        elif p is not None:
                            args.append(slots.get(p, '?tmp%x' % p))
                        else:
                            args.append('?a%d' % i)
                    if fn and fn[0] == 'fn':
                        fname = fn[1]
                    elif fn and fn[0] == 'mem':
                        fname = builtin_var_name(fn[1]) or '?fn@%x' % fn[1]
                    else:
                        fname = '?fn:%s' % (fn,)
                    expr = '%s(%s)' % (fname, ', '.join(args))
                    if ret and ret[0] == 'addr':
                        slots[ret[1]] = expr
                        ret_slots.add(ret[1])
                    regs['eax'] = ret
                    emit(expr)
                except (IndexError, TypeError):
                    emit('?builtin-call (unparsed)')
            elif kind == 'varread':
                # f(&inst, varid, ARRAY_NONE|idx, &out, ...)
                try:
                    inst, vid, idx, outp = pushes[-1], pushes[-2], pushes[-3], pushes[-4]
                    nm = vname(vid[1]) if vid and vid[0] == 'imm' else None
                    iv = val_of(inst)
                    expr = '%s.%s' % (iv, nm or '?var')
                    if idx and idx[0] == 'imm' and idx[1] != 0x80000000:
                        expr += '[%d]' % idx[1]
                    elif idx and idx[0] != 'imm':
                        expr += '[%s]' % val_of(idx)
                    if outp and outp[0] == 'addr':
                        slots[outp[1]] = expr
                except (IndexError, TypeError):
                    pass
            elif kind == 'truthy':
                emit('test %s' % (val_of(pushes[-1]) if pushes else '?'))
            elif kind.startswith('op'):
                # f(&out, &a, b)  b is an int (1 push) or a double (2 pushes) or &rvalue
                try:
                    outp, a = pushes[-1], pushes[-2]
                    rest = pushes[:-2]
                    if len(pushes) >= 4 and a and a[0] == 'imm' and pushes[-3] and pushes[-3][0] == 'imm':
                        av = struct.unpack('<d', struct.pack('<II', pushes[-2][1], pushes[-3][1]))[0]
                        expr = '(%r %s %s)' % (av, kind[2:], const_or_val(pushes[-4]))
                        if outp and outp[0] == 'addr':
                            slots[outp[1]] = expr
                        regs['eax'] = outp
                        pushes = []
                        espargs = None
                        continue
                    if len(rest) >= 2 and rest[-1] and rest[-2] and rest[-1][0] == 'imm' and rest[-2][0] == 'imm':
                        bv = struct.unpack('<d', struct.pack('<II', rest[-1][1], rest[-2][1]))[0]
                        b = repr(bv)
                    else:
                        b = const_or_val(rest[-1]) if rest else '?'
                    expr = '(%s %s %s)' % (val_of(a), kind[2:], b)
                    if outp and outp[0] == 'addr':
                        slots[outp[1]] = expr
                    regs['eax'] = outp
                except (IndexError, TypeError):
                    pass
            elif t in lab and lab[t].startswith(('gml_Script_', 'gml_GlobalScript_')):
                # gml_Script_x(self, other, &ret, argc, argv)
                try:
                    argv, argc, ret = pushes[-5], pushes[-4], pushes[-3]
                    n = argc[1] if argc and argc[0] == 'imm' else 0
                    args = []
                    for i in range(n):
                        p = ptrs.get(argv[1] + 4 * i) if argv and argv[0] == 'addr' else None
                        args.append(p[1] if isinstance(p, tuple) else slots.get(p, '?') if p is not None else '?a%d' % i)
                    expr = '%s(%s)' % (lab[t].replace('gml_Script_', ''), ', '.join(args))
                except (IndexError, TypeError):
                    expr = lab[t].replace('gml_Script_', '') + '(?)'
                    ret = None
                if ret and ret[0] == 'addr':
                    slots[ret[1]] = expr
                regs['eax'] = ret
                emit(expr)
            elif t in lab:
                emit('%s(%s)' % (lab[t], ', '.join(val_of(x) for x in reversed(pushes))))
            elif t not in (0x11a1220, 0x11a1230, 0x1195d50, 0x11a50d0, 0x119c200):
                emit('sub_%x(%s)' % (t, ', '.join(val_of(x) for x in reversed(pushes))))
            pushes = []
            espargs = None
            continue


    if pass_no == 0:
        argv_slots = set(argv_seen)
        local_targets = argv_slots | ret_slots
# print grouped by source line
last = None
for ln, s in out:
    if ln != last:
        print('\nL%d:' % ln)
        last = ln
    print('    ' + s)
