"""Structured lifter for GameMaker 2.3 YYC (x86): compiled GML event/script -> readable pseudo-GML.

v2 adds, over yyclift.py:
  * a control-flow graph with pruning of compiler-generated cold paths (RValue release stubs,
    runtime-error paths), so only GML-level branches remain;
  * structured if / else / while / with output with conditions recovered from
    truthiness tests, value compares (-1/0/1), typed compare wrappers, argument_count tests and
    inlined SSE float compares;
  * array reads/writes (local and instance arrays, constant or computed index);
  * x87 / SSE value tracking for YYC-inlined real arithmetic and float intrinsics
    (lengthdir_x/y, point_direction, abs, sign, min, max, random);
  * script return values.

Output is pseudo-GML for analysis; it is NOT recompilable source and may contain `?` where a value
could not be tracked. Every number printed is read from the binary.

Usage: python yyclift2.py <exe> <labels.json> <vars.json> <asset_index.json> <function> [--asm]
"""
import json
import struct
import sys
from collections import defaultdict

import capstone
from capstone import x86

exe, labels_path, vars_path, assets_path, target = sys.argv[1:6]
SHOW_ASM = '--asm' in sys.argv
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
RDATA = next(s for s in secs if s[0] == '.rdata')
BSS = (DATA[1] + DATA[3], DATA[1] + DATA[2])
GLOBAL_OBJ = 0x166DC10


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


def fmt_num(v):
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def const_rvalue(addr):
    if BSS[0] <= addr < BSS[1]:
        return '0'
    b = rd(addr, 16)
    if len(b) < 16:
        return None
    kind = struct.unpack_from('<I', b, 12)[0] & 0xffffff
    if kind == 0:
        v = struct.unpack_from('<d', b, 0)[0]
        if v != v or abs(v) > 1e300:
            return None
        return fmt_num(v)
    if kind == 5:
        return 'undefined'
    if kind == 13:
        return 'true' if struct.unpack_from('<d', b, 0)[0] else 'false'
    if kind in (7, 10):
        return str(struct.unpack_from('<q' if kind == 10 else '<i', b, 0)[0])
    if kind == 1:
        return '?str@%x' % addr
    return None


def builtin_var_name(a):
    for off in (-4, 0):
        q = rd(a + off, 4)
        if len(q) == 4:
            nm = cstr(struct.unpack('<I', q)[0])
            if nm and nm.replace('_', 'a').replace('@', 'a').isalnum():
                return nm
    return None


def vname(v):
    return var_name.get(v) if v is not None and v >= 100000 else None


# ------------------------------------------------------------------ helper semantics
NOISE = {0x1198F80, 0x1199020, 0x401F20, 0x402270, 0x11A1220, 0x11A1230, 0x11A11F0, 0x1195D50,
         0x11A50D0, 0x1184A20, 0x119C200, 0x11A6CB0, 0x1179C20, 0x11A8C00}
ASSIGN = {0x402130, 0x401FF0, 0x402310}
CMPWRAP = {0x433A90: '==', 0x42F6F0: '<=', 0x5F1F00: '<', 0x948680: '>=', 0x470D80: '>', 0x5F1D40: '>',
           0x412E60: '==', 0x45FCE0: '<'}
CMP_DOUBLE_B = {0x5F1D40, 0x45FCE0}
COMPOUND = {0x40AC40: '*', 0x40DD50: '+', 0x410590: '-', 0x41FC50: '/', 0x4163B0: '+', 0x4394A0: '+', 0x41D3A0: '%'}
COMPOUND_DOUBLE_ARG = {0x4163B0, 0x4394A0}
F_DRAW_SPRITE_EXT = 0x11DA980
F_RANDOM_RANGE = 0x1185470
FLOAT_INTRINSIC = {0x1184D10: ('lengthdir_x', 2), 0x1184DA0: ('lengthdir_y', 2),
                   0x1185380: ('point_direction', 4)}
DOUBLE_INTRINSIC = {0x1184830: 'abs', 0x11854D0: 'sign', 0x1185450: 'random'}
VARIADIC = {0x1184E40: 'max', 0x11850E0: 'min'}
F_CALLBUILTIN, F_TRUTHY, F_VARREAD, F_VARWRITE = 0x119BB40, 0x117B440, 0x119E4D0, 0x119EA60
F_BVREAD = {0x119D2F0, 0x119E2D0, 0x119D180}
F_BVWRITE = {0x119DE50, 0x119E130}
F_OBJVARREAD = 0x119E5D0
F_COMPARE, F_TOREAL, F_TOINT, F_STRING = 0x11C59B0, 0x117CB20, 0x117C630, 0x4055D0
F_ARR_ELEM, F_ARR_LEN, F_ARR_WRITE = 0x11A1200, 0x11A1210, 0x117B170
F_WITH_BEGIN, F_WITH_NEXT, F_WITH_END = 0x119C270, 0x119C630, 0x119C130
F_ERROR = 0x119B610
F_INSTANCE_DESTROY, F_INSTANCE_EXISTS = 0x11AF070, 0x11AF0C0

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True
md.skipdata = True

_op_cache = {}


def classify_op(t):
    """Wrapper f(&out, a, b) = copy a, then compound-op with b. Returns (op, kind_a, kind_b) or None.
    kinds: 'rv' (RValue*), 'int', 'double', 'float' - read from how the wrapper loads [ebp+0xc..]."""
    if t in _op_cache:
        return _op_cache[t]
    res = None
    ins = list(md.disasm(rd(t, 500), t))[:140]
    calls = [int(i.op_str, 16) for i in ins if i.mnemonic == 'call' and i.op_str.startswith('0x')]
    core = calls[1] if len(calls) >= 2 and calls[0] == 0x402310 else None
    op = COMPOUND.get(core)
    if op is None and core is not None:
        cins = [i.mnemonic for i in md.disasm(rd(core, 800), core)][:200]
        for m, o_ in (('divsd', '/'), ('mulsd', '*'), ('subsd', '-'), ('addsd', '+')):
            if m in cins:
                op = o_
                break
    if op:
        kinds = {}
        reg_from = {}
        for i in ins[:40]:
            # int operands are often loaded into a GP register first, then converted
            if i.mnemonic == 'mov' and i.op_str.count('[ebp + 0x') == 1 and i.op_str.split(',')[0] in ('eax', 'ecx', 'edx', 'ebx', 'edi'):
                reg_from[i.op_str.split(',')[0]] = int(i.op_str.split('[ebp + ')[1].split(']')[0], 16)
            if i.mnemonic.startswith('cvtsi2s') and i.op_str.split(', ')[-1] in reg_from:
                kinds.setdefault(reg_from[i.op_str.split(', ')[-1]], 'int')
            if i.mnemonic in ('movd',) and i.op_str.split(', ')[-1] in reg_from:
                kinds.setdefault(reg_from[i.op_str.split(', ')[-1]], 'int')
            for disp in (0xc, 0x10, 0x14):
                if ('[ebp + 0x%x]' % disp) in i.op_str and disp not in kinds:
                    if i.mnemonic.startswith('cvtsi2s'):
                        kinds[disp] = 'int'
                    elif i.mnemonic in ('movsd', 'fld') and 'qword' in i.op_str:
                        kinds[disp] = 'double'
                    elif i.mnemonic in ('cvtss2sd', 'movss') or ('dword' in i.op_str and i.mnemonic == 'fld'):
                        kinds[disp] = 'float'
                    else:
                        kinds[disp] = 'rv'
        ka = 'double' if kinds.get(0xc) == 'double' else ('float' if kinds.get(0xc) == 'float' else 'rv')
        kb_off = 0x14 if ka == 'double' else 0x10
        kb = kinds.get(kb_off, 'rv')
        res = (op, ka, kb)
    _op_cache[t] = res
    return res


def take_arg(P, pos, kind):
    """Consume one cdecl argument from push list P (push order) ending at index pos (negative). Returns (expr, newpos)."""
    if kind == 'double':
        lo, hi = P[pos], P[pos - 1]
        if lo and hi and lo[0] == 'imm' and hi[0] == 'imm':
            return pdouble(lo[1], hi[1]), pos - 2
        return '?dbl', pos - 2
    if kind == 'float':
        v = P[pos]
        if v and v[0] == 'imm':
            return fmt_num(struct.unpack('<f', struct.pack('<I', v[1]))[0]), pos - 1
        return V(v), pos - 1
    if kind == 'int':
        return V(P[pos]), pos - 1
    return C(P[pos]), pos - 1


# ------------------------------------------------------------------ disassembly + CFG
start = int(target, 16) if target.startswith('0x') else by_name[target]
end = min([a for a in lab if a > start and lab[a].startswith(('gml_', 'F_', 'GV_'))] + [start + 0x300000])
insns = [i for i in md.disasm(rd(start, end - start), start) if i.address < end]
idx_of = {ins.address: k for k, ins in enumerate(insns)}


def is_jcc(i):
    return i.mnemonic.startswith('j') and i.mnemonic != 'jmp'


def jump_target(i):
    op = i.operands[0] if i.operands else None
    return op.imm & 0xffffffff if op is not None and op.type == x86.X86_OP_IMM else None


def is_cold_block(addr):
    """Compiler cleanup / runtime-error stubs (not GML control flow)."""
    k = idx_of.get(addr)
    if k is None:
        return True
    for ins in insns[k:k + 14]:
        s = ins.op_str
        if ins.mnemonic == 'call' and s.startswith('0x') and int(s, 16) == F_ERROR:
            return True
        if ins.mnemonic == 'and' and s.endswith('0xffffff') and k + 14 > idx_of.get(ins.address, 0):
            nxt = insns[idx_of[ins.address] + 1] if idx_of[ins.address] + 1 < len(insns) else None
            if nxt is not None and nxt.mnemonic == 'cmp' and nxt.op_str.endswith(', 1'):
                return True
        if ins.mnemonic in ('jmp', 'ret') or is_jcc(ins):
            break
    return False


# successors with pruning
succ = defaultdict(list)
pruned = set()
for k, ins in enumerate(insns):
    nxt = insns[k + 1].address if k + 1 < len(insns) else None
    if ins.mnemonic == 'ret':
        continue
    if ins.mnemonic == 'jmp':
        t = jump_target(ins)
        if t is not None and not is_cold_block(t):
            succ[ins.address].append(t)
        elif t is not None:
            pruned.add(ins.address)
        continue
    if is_jcc(ins):
        t = jump_target(ins)
        # flags come from the closest earlier flag-setting instruction (mov/lea/push don't touch flags)
        j_ = k - 1
        while j_ > 0 and insns[j_].mnemonic in ('mov', 'lea', 'push', 'movsd', 'movss', 'movaps') and k - j_ < 6:
            j_ -= 1
        prev = insns[j_] if k else None
        cleanup_test = prev is not None and prev.mnemonic == 'test' and prev.op_str.endswith('0xfffffc')
        kind_test = prev is not None and prev.mnemonic == 'test' and prev.op_str.endswith(', 0xffffff')             and prev.op_str.startswith('dword ptr [')
        if t is not None and kind_test and ins.mnemonic in ('je', 'jne'):
            # RValue type dispatch (real payload vs. conversion): one GML value, keep one path
            pruned.add(ins.address)
            if ins.mnemonic == 'je':
                succ[ins.address].append(t)
            elif nxt is not None:
                succ[ins.address].append(nxt)
            continue
        if t is not None and cleanup_test and ins.mnemonic == 'jne':
            pruned.add(ins.address)              # jne <continue> ; <fallthrough = cold release stub>
            succ[ins.address].append(t)
            continue
        if t is not None:
            if cleanup_test or is_cold_block(t):
                pruned.add(ins.address)          # only the fall-through path is real GML flow
            elif nxt is not None and is_cold_block(nxt):
                pruned.add(ins.address)
                succ[ins.address].append(t)
                continue
            else:
                succ[ins.address].append(t)
        if nxt is not None:
            succ[ins.address].append(nxt)
        continue
    if nxt is not None:
        succ[ins.address].append(nxt)

reach = set()
stack = [start]
while stack:
    a = stack.pop()
    if a in reach or a not in idx_of:
        continue
    reach.add(a)
    stack.extend(succ[a])

def is_exit_path(t):
    """True if from t only cleanup code runs before `ret` (YYC shared epilogue)."""
    k = idx_of.get(t)
    steps = 0
    while k is not None and k < len(insns) and steps < 2000:
        i_ = insns[k]
        if i_.mnemonic == 'ret':
            return True
        if i_.mnemonic == 'call' and i_.op_str.startswith('0x') and int(i_.op_str, 16) not in NOISE | {0x402270, 0x401F20}:
            return False
        if i_.mnemonic == 'call' and not i_.op_str.startswith('0x'):
            k += 1
            steps += 1
            continue
        if i_.mnemonic == 'jmp':
            tt = jump_target(i_)
            if tt is None or tt <= i_.address:
                return False
            k = idx_of.get(tt)
        else:
            k += 1
        steps += 1
    return False


EXIT_CACHE = {}


def exit_path(t):
    if t not in EXIT_CACHE:
        EXIT_CACHE[t] = is_exit_path(t)
    return EXIT_CACHE[t]


# predecessor counts (for join detection)
preds = defaultdict(int)
for a in reach:
    for s in succ[a]:
        if s in reach:
            preds[s] += 1

# ------------------------------------------------------------------ value tracking state
regs = {}            # gp register -> value tuple
xmm = {}             # xmm reg -> expr
st0 = [None]         # x87 top
slots = {}           # frame offset -> expr
spill = {}           # frame offset -> raw tracked value (non-RValue spills: element pointers etc.)
ptrs = {}            # frame offset -> frame offset (argv entries)
rawd = {}
vstore = {}
pushes = []
espargs = None
line_slot = None
cur_line = [0]
pending_cond = [None]     # (kind, data) of the last condition-producing op
skip_jcc = [False]
abs_src = [None]
incdec = [None]
argv_seen = set()
ret_slots = set()
out = []
indent = [0]


def emit(s, comment=None):
    if chain and not s.startswith('if ('):
        flush_chain()
    pad = '    ' * indent[0]
    out.append(pad + s + (('  // L%d' % cur_line[0]) if comment is None and cur_line[0] else ''))


def V(x):
    """value tuple -> expression string."""
    if x is None:
        return '?'
    k = x[0]
    if k == 'addr':
        if x[1] in slots:
            return slots[x[1]]
        lo, hi, kd = rawd.get(x[1]), rawd.get(x[1] + 4), rawd.get(x[1] + 12)
        if lo is not None and hi is not None and kd in (0, None):
            return fmt_num(struct.unpack('<d', struct.pack('<II', lo, hi))[0])
        return 'local_%x' % x[1]
    if k == 'imm':
        v = x[1]
        return str(v - (1 << 32)) if 0x80000000 < v <= 0xffffffff else str(v)
    if k in ('ref', 'expr', 'varref', 'elemref'):
        return x[1]
    if k == 'const':
        return x[1]
    if k == 'mem':
        return builtin_var_name(x[1]) or '?mem%x' % x[1]
    return '?'


def C(x):
    """value tuple that may be an address of a constant RValue."""
    if x and x[0] == 'imm' and RDATA[1] <= x[1] < BSS[1]:
        c = const_rvalue(x[1])
        if c is not None:
            return c
    return V(x)


def pdouble(lo, hi):
    return fmt_num(struct.unpack('<d', struct.pack('<II', lo, hi))[0])


def push_val(op, ins):
    if op.type == x86.X86_OP_IMM:
        return ('imm', op.imm & 0xffffffff)
    if op.type == x86.X86_OP_REG:
        return regs.get(ins.reg_name(op.reg))
    m = op.mem
    if m.base == 0 and m.index == 0:
        a = m.disp & 0xffffffff
        if (a - 4) in lab and lab[a - 4].startswith('g_FUNC_'):
            return ('fn', lab[a - 4][7:])
        return ('mem', a)
    bn = ins.reg_name(m.base)
    if bn == 'ebp' and m.disp == 8:
        return ('ref', 'self')
    if bn == 'ebp' and m.disp == 0xc:
        return ('ref', 'other')
    if bn == 'esi':
        if m.disp in ptrs:
            p = ptrs[m.disp]
            return ('expr', p[1]) if isinstance(p, tuple) else ('addr', p)
        if m.disp in spill:
            return spill[m.disp]
        return ('arrbase', m.disp)
    return None


def is_local_target(off):
    return off not in argv_seen and off >= 0x40


def assign_slot(off, v, quiet=False):
    if v == 'undefined' and str(slots.get(off, '')).startswith('argument'):
        return
    if not quiet and is_local_target(off) and v and not v.startswith('local_%x' % off):
        emit('local_%x = %s' % (off, v))
        slots[off] = 'local_%x' % off
    else:
        slots[off] = v


JCC_FROM_CMP = {   # jump taken when ... ; we return the condition for the FALLTHROUGH (body) path
    'je': '!=', 'jne': '==', 'jle': '>', 'jl': '>=', 'jge': '<', 'jg': '<=',
    'jbe': '>', 'jb': '>=', 'jae': '<', 'ja': '<=', 'js': '>=', 'jns': '<',
}


JCC_TAKEN = {'je': '==', 'jne': '!=', 'jle': '<=', 'jl': '<', 'jge': '>=', 'jg': '>',
             'jbe': '<=', 'jb': '<', 'jae': '>=', 'ja': '>', 'js': '<', 'jns': '>='}


def taken_condition(ins):
    pc = pending_cond[0]
    m = ins.mnemonic
    if pc is None:
        return '!(?cond@%x)' % ins.address
    kind, data = pc
    if kind == 'bool':
        return '!(%s)' % data if m == 'je' else (data if m == 'jne' else '?')
    op = JCC_TAKEN.get(m)
    if kind in ('cmp', 'fcmp'):
        return '%s %s %s' % (data[0], op, data[1]) if op else '?'
    if kind == 'argc':
        return 'argument_count %s %d' % (op, data) if op else '?'
    return '?'


def body_condition(ins):
    """Condition under which the fall-through (body) executes, given pending condition state."""
    pc = pending_cond[0]
    m = ins.mnemonic
    if pc is None:
        return '?cond@%x' % ins.address
    kind, data = pc
    if kind == 'bool':      # al = data ; test al,al ; je -> body when data true
        return data if m == 'je' else ('!(%s)' % data if m == 'jne' else '?%s(%s)' % (m, data))
    if kind == 'cmp':       # r = compare(a,b) ; test r,r ; jcc
        a, b = data
        op = JCC_FROM_CMP.get(m)
        return '%s %s %s' % (a, op, b) if op else '?%s cmp(%s,%s)' % (m, a, b)
    if kind == 'fcmp':      # comisd a, b ; jcc (unsigned flags)
        a, b = data
        op = JCC_FROM_CMP.get(m)
        return '%s %s %s' % (a, op, b) if op else '?%s fcmp(%s,%s)' % (m, a, b)
    if kind == 'argc':
        n = data
        op = JCC_FROM_CMP.get(m)
        return 'argument_count %s %d' % (op, n) if op else '?argc'
    return '?cond'


# pre-pass: frame slots whose address is stored into an argv array are call temporaries, not locals
_lea = {}
for a in sorted(reach):
    i_ = insns[idx_of[a]]
    if i_.mnemonic == 'lea' and i_.operands[1].type == x86.X86_OP_MEM and i_.operands[1].mem.base \
            and i_.reg_name(i_.operands[1].mem.base) == 'esi':
        _lea[i_.reg_name(i_.operands[0].reg)] = i_.operands[1].mem.disp
    elif i_.mnemonic == 'mov' and len(i_.operands) == 2 and i_.operands[0].type == x86.X86_OP_MEM \
            and i_.operands[0].mem.base and i_.reg_name(i_.operands[0].mem.base) == 'esi' \
            and i_.operands[1].type == x86.X86_OP_REG and i_.reg_name(i_.operands[1].reg) in _lea:
        argv_seen.add(_lea[i_.reg_name(i_.operands[1].reg)])
    elif i_.mnemonic in ('call', 'jmp') or is_jcc(i_):
        _lea.clear()

chain = []
chain_len = [0]


def wrap(e):
    return e if (e.startswith('(') and e.endswith(')')) or ' ' not in e else '(%s)' % e


def flush_chain():
    if not chain:
        return
    items = chain[:]
    chain.clear()
    last = items[-1]
    skip, body = last['t'], last['after']
    if all(it['t'] in (skip, body) for it in items[:-1]):
        e = last['fall']
        for it in reversed(items[:-1]):
            e = ('%s || %s' % (wrap(it['taken']), wrap(e))) if it['t'] == body else ('%s && %s' % (wrap(it['fall']), wrap(e)))
        saved = cur_line[0]
        cur_line[0] = items[0]['line']
        emit('if (%s) {' % e)
        cur_line[0] = saved
        indent[0] += 1
        blocks.append({'kind': 'if', 'end': skip, 'start': items[0]['after'], 'snap': dict(slots), 'rsnap': dict(rawd), 'cond': e})
    else:
        # not a simple short-circuit chain: nest
        for it in items:
            saved = cur_line[0]
            cur_line[0] = it['line']
            emit('if (%s) {' % it['fall'])
            cur_line[0] = saved
            indent[0] += 1
            blocks.append({'kind': 'if', 'end': it['t'], 'start': it['after'], 'snap': dict(slots), 'rsnap': dict(rawd), 'cond': it['fall']})


def snap_raw_get(b, k):
    return b.get('rsnap', {}).get(k)


# ------------------------------------------------------------------ structured walk
blocks = []      # stack of dicts: kind, end, start, cond_start
reach_sorted = sorted(reach)
loop_headers = set()
for a in reach_sorted:
    ins = insns[idx_of[a]]
    t = jump_target(ins) if (ins.mnemonic == 'jmp' or is_jcc(ins)) else None
    if t is not None and t <= a and t in reach and ins.address not in pruned and not exit_path(t):
        loop_headers.add(t)

prev_reached = None
for a in reach_sorted:
    ins = insns[idx_of[a]]
    # a pending condition chain whose body starts here (or that we've run past) becomes an if
    if chain and (a in {it['t'] for it in chain} or (blocks and a >= blocks[-1]['end']) or a in loop_headers):
        flush_chain()
    # close finished blocks
    while blocks and a >= blocks[-1]['end']:
        b = blocks.pop()
        indent[0] -= 1
        snap = b.get('snap')
        if snap is not None:
            for k_ in list(slots):
                if snap.get(k_) != slots[k_] and k_ not in argv_seen:
                    slots[k_] = 'local_%x' % k_
            for k_ in list(rawd):
                if snap_raw_get(b, k_) != rawd[k_]:
                    rawd.pop(k_, None)
        if b.get('else_end') and a == b['end'] and b['else_end'] > a:
            emit('} else {', comment='')
            indent[0] += 1
            blocks.append({'kind': 'else', 'end': b['else_end'], 'start': a, 'snap': dict(slots), 'rsnap': dict(rawd)})
            break
        emit('}', comment='')
    if preds.get(a, 0) > 1:
        # join point: register contents depend on the path taken. Script-argument values survive
        # (the other path is the 'undefined' default for a missing argument).
        keep = {k: v for k, v in regs.items() if v and v[0] == 'expr' and str(v[1]).startswith('argument')}
        regs.clear()
        regs.update(keep)
        xmm.clear()
        st0[0] = None
    if a in loop_headers and not any(b.get('header') == a for b in blocks):
        emit('loop {  // header @%x' % a, comment='')
        indent[0] += 1
        latch = max(x for x in reach_sorted if (jump_target(insns[idx_of[x]]) == a and x >= a))
        blocks.append({'kind': 'loop', 'end': latch + insns[idx_of[latch]].size, 'start': a, 'header': a})
    if SHOW_ASM:
        out.append('    ' * indent[0] + '; %08x %-6s %-40s | x0=%s x1=%s st0=%s eax=%s' % (
            ins.address, ins.mnemonic, ins.op_str, xmm.get('xmm0'), xmm.get('xmm1'), st0[0], regs.get('eax')))

    mn, ops = ins.mnemonic, ins.operands

    # ---- esp argument frames
    if mn == 'sub' and ins.op_str.startswith('esp, '):
        espargs = {}
        continue
    if mn in ('mov', 'movsd', 'movss') and len(ops) == 2 and ops[0].type == x86.X86_OP_MEM \
            and ops[0].mem.base and ins.reg_name(ops[0].mem.base) == 'esp' and espargs is not None:
        s = ops[1]
        if s.type == x86.X86_OP_REG:
            rn = ins.reg_name(s.reg)
            espargs[ops[0].mem.disp] = ('expr', xmm.get(rn, '?xmm')) if rn.startswith('xmm') else regs.get(rn)
        elif s.type == x86.X86_OP_IMM:
            espargs[ops[0].mem.disp] = ('imm', s.imm & 0xffffffff)
        continue
    if mn == 'fstp' and ops and ops[0].type == x86.X86_OP_MEM and ops[0].mem.base:
        bn = ins.reg_name(ops[0].mem.base)
        if bn == 'esp' and espargs is not None:
            espargs[ops[0].mem.disp] = ('expr', st0[0] or '?st0')
        elif bn == 'esi':
            assign_slot(ops[0].mem.disp, st0[0] or '?st0', quiet=True)
        st0[0] = None
        continue
    if mn == 'fld' and ops and ops[0].type == x86.X86_OP_MEM:
        m = ops[0].mem
        if m.base and ins.reg_name(m.base) == 'esi':
            st0[0] = V(('addr', m.disp))
        elif m.base == 0:
            b = rd(m.disp & 0xffffffff, 8)
            st0[0] = fmt_num(struct.unpack('<d', b)[0]) if len(b) == 8 else '?'
        continue

    # ---- SSE value tracking
    if mn in ('movsd', 'movss', 'movaps', 'movapd', 'cvtss2sd', 'cvtsd2ss', 'cvtps2pd', 'cvtpd2ps', 'movq') and len(ops) == 2 \
            and ops[0].type == x86.X86_OP_REG and ins.reg_name(ops[0].reg).startswith('xmm'):
        dst = ins.reg_name(ops[0].reg)
        s = ops[1]
        if s.type == x86.X86_OP_REG:
            xmm[dst] = xmm.get(ins.reg_name(s.reg), '?')
        elif s.mem.base == 0:
            b = rd(s.mem.disp & 0xffffffff, 8 if mn != 'movss' else 4)
            if len(b) == 8:
                xmm[dst] = fmt_num(struct.unpack('<d', b)[0])
            elif len(b) == 4:
                xmm[dst] = fmt_num(struct.unpack('<f', b)[0])
        else:
            bn = ins.reg_name(s.mem.base)
            if bn == 'esi':
                xmm[dst] = V(('addr', s.mem.disp))
            elif bn == 'esp' and espargs is not None and s.mem.disp in espargs:
                xmm[dst] = V(espargs[s.mem.disp])
            elif regs.get(bn) and regs[bn][0] in ('varref', 'elemref', 'expr') and s.mem.disp == 0:
                xmm[dst] = regs[bn][1]
            else:
                xmm[dst] = '?'
        continue
    if mn in ('cvtsi2sd', 'cvtsi2ss', 'cvtdq2pd', 'cvtdq2ps') and ops[0].type == x86.X86_OP_REG:
        s = ops[1]
        xmm[ins.reg_name(ops[0].reg)] = V(regs.get(ins.reg_name(s.reg))) if s.type == x86.X86_OP_REG else '?int'
        continue
    if mn in ('xorps', 'xorpd', 'pxor') and ops[1].type == x86.X86_OP_REG and ops[0].reg == ops[1].reg:
        xmm[ins.reg_name(ops[0].reg)] = '0'
        continue
    # inlined abs(): x87-free sign-mask select  (cmpltsd m,0 ; andpd/andnpd ; orpd)
    if mn in ('cmpltsd', 'cmpltss'):
        abs_src[0] = xmm.get(ins.reg_name(ops[0].reg), '?')
        continue
    if mn in ('orpd', 'orps') and abs_src[0] is not None:
        xmm[ins.reg_name(ops[0].reg)] = 'abs(%s)' % abs_src[0]
        abs_src[0] = None
        continue
    if mn in ('andpd', 'andnpd', 'andps', 'andnps', 'xorpd', 'xorps'):
        continue
    if mn in ('movapd', 'movaps') and ops[0].type == x86.X86_OP_MEM and ops[0].mem.base and ins.reg_name(ops[0].mem.base) == 'esi':
        assign_slot(ops[0].mem.disp, xmm.get(ins.reg_name(ops[1].reg), '?'), quiet=True)
        continue
    if mn in ('addsd', 'subsd', 'mulsd', 'divsd', 'addss', 'subss', 'mulss', 'divss', 'minsd', 'maxsd'):
        dst = ins.reg_name(ops[0].reg)
        s = ops[1]
        if s.type == x86.X86_OP_REG:
            bv = xmm.get(ins.reg_name(s.reg), '?')
        elif s.mem.base == 0:
            b = rd(s.mem.disp & 0xffffffff, 8 if mn.endswith('sd') else 4)
            bv = fmt_num(struct.unpack('<d' if len(b) == 8 else '<f', b)[0]) if len(b) in (4, 8) else '?'
        elif ins.reg_name(s.mem.base) == 'esi':
            bv = V(('addr', s.mem.disp))
        else:
            bv = '?'
        sym = {'add': '+', 'sub': '-', 'mul': '*', 'div': '/', 'min': 'min', 'max': 'max'}[mn[:3]]
        av = xmm.get(dst, '?')
        xmm[dst] = ('%s(%s, %s)' % (sym, av, bv)) if sym in ('min', 'max') else '(%s %s %s)' % (av, sym, bv)
        continue
    if mn in ('comisd', 'ucomisd', 'comiss', 'ucomiss'):
        s = ops[1]
        if s.type == x86.X86_OP_REG:
            bv = xmm.get(ins.reg_name(s.reg), '?')
        elif s.mem.base == 0:
            b = rd(s.mem.disp & 0xffffffff, 8)
            bv = fmt_num(struct.unpack('<d', b)[0]) if len(b) == 8 else '?'
        elif ins.reg_name(s.mem.base) == 'esi':
            bv = V(('addr', s.mem.disp))
        else:
            bv = '?'
        pending_cond[0] = ('fcmp', (xmm.get(ins.reg_name(ops[0].reg), '?'), bv))
        continue
    if mn in ('movsd', 'movss') and ops[0].type == x86.X86_OP_MEM and ops[0].mem.base and ins.reg_name(ops[0].mem.base) == 'esi':
        assign_slot(ops[0].mem.disp, xmm.get(ins.reg_name(ops[1].reg), '?'), quiet=True)
        continue

    # ---- direct RValue stores into a variable ref / element ref
    if mn in ('mov', 'movsd') and len(ops) == 2 and ops[0].type == x86.X86_OP_MEM and ops[0].mem.base \
            and (regs.get(ins.reg_name(ops[0].mem.base)) or (None,))[0] in ('varref', 'elemref'):
        rn = ins.reg_name(ops[0].mem.base)
        ref = regs[rn][1]
        st = vstore.setdefault(rn, {})
        s = ops[1]
        if s.type == x86.X86_OP_IMM:
            st[ops[0].mem.disp] = s.imm & 0xffffffff
        elif s.type == x86.X86_OP_REG and ins.reg_name(s.reg).startswith('xmm'):
            st['dbl'] = xmm.get(ins.reg_name(s.reg), '?')
        kd = st.get(0xc)
        if isinstance(kd, int) and ('dbl' in st or (kd & 0xffffff) == 5 or (isinstance(st.get(0), int) and isinstance(st.get(4), int))):
            kind = kd & 0xffffff
            if 'dbl' in st:
                v = st['dbl']
            elif kind == 5:
                v = 'undefined'
            elif kind == 0:
                v = pdouble(st[0], st[4])
            elif kind == 13:
                v = 'true' if (st[0] or st[4]) else 'false'
            elif kind in (7, 10):
                v = str(st[0])
            else:
                v = '?kind%d' % kind
            emit('%s = %s' % (ref, v))
            vstore.pop(rn, None)
        continue

    # ---- stack-trace line marker
    if mn == 'mov' and len(ops) == 2 and ops[0].type == x86.X86_OP_MEM and ops[1].type == x86.X86_OP_IMM \
            and ops[0].mem.base and ins.reg_name(ops[0].mem.base) == 'esi':
        disp, imm = ops[0].mem.disp, ops[1].imm & 0xffffffff
        if line_slot is None and (cstr(imm) or '').startswith('gml_'):
            line_slot = disp + 4
            continue
        if disp == line_slot:
            cur_line[0] = imm
            continue
        rawd[disp] = imm
        slots.pop(disp, None) if disp in slots and (disp + 12) in rawd else None
        continue
    if mn == 'lea' and ops[1].type == x86.X86_OP_MEM and ops[1].mem.base and ins.reg_name(ops[1].mem.base) == 'esi':
        regs[ins.reg_name(ops[0].reg)] = ('addr', ops[1].mem.disp)
        continue
    if mn == 'cmp' and len(ops) == 2 and ops[0].type == x86.X86_OP_MEM and ops[0].mem.base \
            and ins.reg_name(ops[0].mem.base) == 'ebp' and ops[0].mem.disp == 0x14 and ops[1].type == x86.X86_OP_IMM:
        pending_cond[0] = ('argc', ops[1].imm)
        continue
    if mn == 'cmp' and len(ops) == 2 and ops[0].type == x86.X86_OP_REG and ops[1].type == x86.X86_OP_IMM             and (ops[1].imm & 0xffffffff) == 0xfffffffe and (regs.get(ins.reg_name(ops[0].reg)) or (None,))[0] == 'cmpres':
        skip_jcc[0] = True
        continue
    if mn == 'test' and len(ops) == 2 and ops[0].type == x86.X86_OP_REG and ops[1].type == x86.X86_OP_REG and ops[0].reg == ops[1].reg:
        rn = ins.reg_name(ops[0].reg)
        v = regs.get(rn) if rn != 'al' else regs.get('eax')
        if v and v[0] == 'cmpres':
            pending_cond[0] = ('cmp', v[1])
        elif v and v[0] == 'boolres':
            pending_cond[0] = ('bool', v[1])
        continue
    if mn == 'mov' and len(ops) == 2:
        dst, s = ops
        if dst.type == x86.X86_OP_REG:
            rn = ins.reg_name(dst.reg)
            if s.type == x86.X86_OP_MEM:
                m = s.mem
                bn = ins.reg_name(m.base) if m.base else None
                if bn == 'ebp' and m.disp == 0x18:
                    regs[rn] = ('argv',)
                elif bn == 'ebp' and m.disp == 0x10:
                    regs[rn] = ('retslot',)
                elif bn and (regs.get(bn) or (None,))[0] == 'argv':
                    regs[rn] = ('expr', 'argument%d' % (m.disp // 4))
                elif bn == 'ebp' and m.disp == 8:
                    regs[rn] = ('ref', 'self')
                elif bn == 'ebp' and m.disp == 0xc:
                    regs[rn] = ('ref', 'other')
                elif bn is None and (m.disp & 0xffffffff) == GLOBAL_OBJ:
                    regs[rn] = ('ref', 'global')
                elif bn is None:
                    regs[rn] = ('mem', m.disp & 0xffffffff)
                elif bn and (regs.get(bn) or (None,))[0] == 'ref' and m.disp == 0:
                    regs[rn] = ('vtbl', regs[bn][1])
                elif bn and (regs.get(bn) or (None,))[0] == 'vtbl' and m.disp in (4, 8):
                    regs[rn] = ('vtblfn', regs[bn][1])
                elif bn == 'esi':
                    if m.disp in spill and spill[m.disp] and spill[m.disp][0] in ('varref', 'elemref'):
                        regs[rn] = spill[m.disp]
                    elif m.disp in ptrs:
                        p = ptrs[m.disp]
                        regs[rn] = ('expr', p[1]) if isinstance(p, tuple) else ('addr', p)
                    elif m.disp in spill:
                        regs[rn] = spill[m.disp]
                    else:
                        regs[rn] = ('arrbase', m.disp)      # payload of an RValue slot (array pointer etc.)
                else:
                    regs[rn] = None
            elif s.type == x86.X86_OP_REG:
                regs[rn] = regs.get(ins.reg_name(s.reg))
            else:
                cur = regs.get(rn)
                if (s.imm & 0xffffffff) == 0x15DD750 and cur and cur[0] == 'expr' and str(cur[1]).startswith('argument'):
                    pass    # default-argument 'undefined' on the missing-argument path
                else:
                    regs[rn] = ('imm', s.imm & 0xffffffff)
        elif dst.type == x86.X86_OP_MEM and dst.mem.base and ins.reg_name(dst.mem.base) == 'esi' and s.type == x86.X86_OP_REG:
            v = regs.get(ins.reg_name(s.reg))
            if v and v[0] == 'addr':
                ptrs[dst.mem.disp] = v[1]
                spill.pop(dst.mem.disp, None)
                argv_seen.add(v[1])
            elif v and v[0] in ('varref', 'elemref'):
                ptrs[dst.mem.disp] = ('var', v[1])
                spill[dst.mem.disp] = v
            else:
                ptrs.pop(dst.mem.disp, None)
                if v is not None:
                    spill[dst.mem.disp] = v
                else:
                    spill.pop(dst.mem.disp, None)
        continue
    if mn == 'push':
        pushes.append(push_val(ops[0], ins))
        continue
    if mn == 'add' and ins.op_str.startswith('esp'):
        pushes = []
        continue

    # ---- branches
    if mn == 'jmp':
        flush_chain()
        t = jump_target(ins)
        if t is None:
            continue
        if exit_path(t) and not (blocks and t == blocks[-1]['end']):
            emit('exit', comment='')
            continue
        loops = [b for b in blocks if b['kind'] == 'loop']
        if loops and t == loops[-1]['header']:
            nxt_idx = idx_of[a] + 1
            if nxt_idx < len(insns) and insns[nxt_idx].address >= loops[-1]['end']:
                pass  # latch of the loop itself
            else:
                emit('continue', comment='')
        elif loops and t >= loops[-1]['end'] and t > a:
            emit('break', comment='')
        elif blocks and blocks[-1]['kind'] == 'if' and t > blocks[-1]['end'] and \
                insns[idx_of[a] + 1].address == blocks[-1]['end'] if idx_of[a] + 1 < len(insns) else False:
            blocks[-1]['else_end'] = t
        continue
    if is_jcc(ins) and skip_jcc[0]:
        skip_jcc[0] = False      # 'compare == -2' (incomparable) guard; same target as the real test
        continue
    if is_jcc(ins):
        t = jump_target(ins)
        if ins.address in pruned or t is None:
            pending_cond[0] = None
            continue
        cond = body_condition(ins)
        tcond = taken_condition(ins)
        pending_cond[0] = None
        if t > a:
            chain.append({'fall': cond, 'taken': tcond, 't': t, 'after': ins.address + ins.size, 'line': cur_line[0]})
            chain_len[0] = len(out)
            continue
        else:
            emit('if (!(%s)) continue-to @%x' % (cond, t), comment='')
        continue

    if mn == 'ret':
        emit('exit', comment='')
        continue

    # ---- calls
    if mn != 'call':
        continue
    op = ops[0]
    if op.type == x86.X86_OP_MEM and op.mem.disp in (4, 8) and op.mem.index == 0:
        owner = regs.get('ecx')
        base_v = regs.get(ins.reg_name(op.mem.base)) if op.mem.base else None
        if owner and owner[0] == 'ref':
            on = owner[1]
        elif base_v and base_v[0] == 'vtbl':
            on = base_v[1]
        else:
            on = '?inst'
        vid = pushes[-1][1] if pushes and pushes[-1] and pushes[-1][0] == 'imm' else None
        nm = vname(vid) or '?var%s' % vid
        regs['eax'] = ('varref', nm if on == 'self' else '%s.%s' % (on, nm))
        pushes = []
        continue
    if op.type == x86.X86_OP_REG and (regs.get(ins.reg_name(op.reg)) or (None,))[0] == 'vtblfn':
        on = regs[ins.reg_name(op.reg)][1]
        vid = pushes[-1][1] if pushes and pushes[-1] and pushes[-1][0] == 'imm' else None
        nm = vname(vid) or '?var%s' % vid
        regs['eax'] = ('varref', nm if on == 'self' else '%s.%s' % (on, nm))
        pushes = []
        continue
    if op.type != x86.X86_OP_IMM:
        pushes = []
        espargs = None
        continue
    t = op.imm & 0xffffffff
    P = pushes
    try:
        if t in NOISE:
            pass
        elif t in ASSIGN:
            dest = regs.get('ecx')
            src = P[-1] if P else None
            v = C(src)
            if dest and dest[0] == 'addr' and src and src[0] == 'addr' and src[1] in ret_slots and dest[1] != src[1]                     and '(' in v:
                emit('local_%x = %s' % (dest[1], v))
                slots[dest[1]] = 'local_%x' % dest[1]
            elif dest and dest[0] == 'addr':
                assign_slot(dest[1], v)
            elif dest and dest[0] in ('varref', 'elemref'):
                if not (incdec[0] and dest[1].endswith(incdec[0].split('.')[-1])):
                    emit('%s = %s' % (dest[1], v))
                incdec[0] = None
            elif dest and dest[0] == 'retslot':
                emit('return %s' % v)
            elif dest and dest[0] == 'arrbase':
                assign_slot(dest[1], v)
        elif t == F_CALLBUILTIN:
            argv, fn, argc, ret = P[-6], P[-5], P[-4], P[-3]
            n = argc[1] if argc and argc[0] == 'imm' else 0
            args = []
            for i in range(n):
                p = ptrs.get(argv[1] + 4 * i) if argv and argv[0] == 'addr' else None
                if isinstance(p, tuple):
                    args.append(p[1])
                elif p is not None:
                    args.append(V(('addr', p)))
                else:
                    args.append('?')
            fname = fn[1] if fn and fn[0] == 'fn' else (builtin_var_name(fn[1]) if fn and fn[0] == 'mem' else None) or '?fn'
            if fname in ('@@This@@', '@@Other@@', '@@Global@@'):
                expr = {'@@This@@': 'self', '@@Other@@': 'other', '@@Global@@': 'global'}[fname]
                regs['eax'] = ('ref', expr)
                if ret and ret[0] == 'addr':
                    slots[ret[1]] = expr
                pushes = []
                espargs = None
                continue
            expr = '%s(%s)' % (fname, ', '.join(args))
            if ret and ret[0] == 'addr':
                slots[ret[1]] = expr
                ret_slots.add(ret[1])
            regs['eax'] = ret
            emit(expr)
        elif t == F_TRUTHY:
            regs['eax'] = ('boolres', V(P[-1]) if P else '?')
        elif t == F_COMPARE and espargs:
            regs['eax'] = ('cmpres', (V(espargs.get(0)), V(espargs.get(4))))
        elif t in CMPWRAP:
            a_, b_ = P[-1], P[-2]
            bv = take_arg(P, -2, 'double')[0] if t in CMP_DOUBLE_B and len(P) >= 3 else C(b_)
            regs['eax'] = ('boolres', '%s %s %s' % (V(a_), CMPWRAP[t], bv))
        elif t == F_VARREAD:
            inst, vid, ix, outp = P[-1], P[-2], P[-3], P[-4]
            nm = vname(vid[1]) if vid and vid[0] == 'imm' else '?var'
            e = '%s.%s' % (V(inst), nm)
            if ix and ix[0] == 'imm' and ix[1] != 0x80000000:
                e += '[%d]' % ix[1]
            elif ix and ix[0] != 'imm':
                e += '[%s]' % V(ix)
            if outp and outp[0] == 'addr':
                slots[outp[1]] = e
        elif t in (F_VARWRITE,) or t in F_BVWRITE or t in F_BVREAD:
            inst, vid, ix, v = P[-1], P[-2], P[-3], P[-4]
            if vid and vid[0] == 'imm':
                nm = vname(vid[1]) or '?var%d' % vid[1]
            elif vid and vid[0] == 'mem':
                nm = builtin_var_name(vid[1]) or '?bvar'
            else:
                nm = '?var'
            iv = V(inst)
            lhs = nm if iv == 'self' else '%s.%s' % (iv, nm)
            if ix and ix[0] == 'imm' and ix[1] != 0x80000000:
                lhs += '[%d]' % (ix[1] - (1 << 32) if ix[1] > 0x7fffffff else ix[1])
            elif ix and ix[0] != 'imm':
                lhs += '[%s]' % V(ix)
            if t in F_BVREAD:
                if v and v[0] == 'addr':
                    slots[v[1]] = lhs
            elif incdec[0] and lhs.endswith(incdec[0].split('.')[-1]):
                incdec[0] = None
            else:
                emit('%s = %s' % (lhs, C(v) if v and v[0] == 'imm' and v[1] > 0x10000 else V(v)))
        elif t == F_OBJVARREAD:
            obj, vid, ix, outp = P[-1], P[-2], P[-3], P[-4]
            nm = vname(vid[1]) if vid and vid[0] == 'imm' else '?var'
            o_ = V(obj)
            if obj and obj[0] == 'imm':
                o_ = 'obj%d' % obj[1]
            if outp and outp[0] == 'addr':
                slots[outp[1]] = '%s.%s' % (o_, nm)
        elif t == F_STRING:
            dest = regs.get('ecx')
            a_ = P[-1][1] if P and P[-1] and P[-1][0] in ('imm', 'mem') else None
            txt = cstr(a_) if a_ else None
            if txt is None and a_:
                q = rd(a_, 4)
                txt = cstr(struct.unpack('<I', q)[0]) if len(q) == 4 else None
            if dest and dest[0] == 'addr':
                slots[dest[1]] = '"%s"' % txt if txt is not None else '?str'
        elif t == F_TOREAL:
            st0[0] = V(P[-1]) if P else '?'
        elif t == F_TOINT:
            regs['eax'] = ('expr', V(P[-1]) if P else '?')
        elif t == F_ARR_LEN:
            regs['eax'] = ('expr', 'array_length(%s)' % V(P[-1]))
        elif t == F_ARR_ELEM:
            arr, ix = P[-1], P[-2]
            an = 'local_%x' % arr[1] if arr and arr[0] == 'arrbase' else V(arr)
            if arr and arr[0] == 'arrbase' and arr[1] in slots:
                an = slots[arr[1]]
            regs['eax'] = ('elemref', '%s[%s]' % (an, V(ix)))
        elif t == F_ARR_WRITE:
            arrp, ix = P[-1], P[-2]
            an = slots.get(arrp[1], 'local_%x' % arrp[1]) if arrp and arrp[0] == 'addr' else V(arrp)
            regs['eax'] = ('elemref', '%s[%s]' % (an, V(ix)))
        elif t in COMPOUND:
            dest = regs.get('ecx')
            if t in COMPOUND_DOUBLE_ARG and len(P) >= 2:
                src, _ = take_arg(P, -1, 'double')
            else:
                src = C(P[-1]) if P else '?'
            if dest and dest[0] == 'addr':
                assign_slot(dest[1], '(%s %s %s)' % (V(dest), COMPOUND[t], src), quiet=True)
            elif dest and dest[0] in ('varref', 'elemref'):
                emit('%s %s= %s' % (dest[1], COMPOUND[t], src))
            regs['eax'] = dest
        elif t in FLOAT_INTRINSIC and espargs is not None:
            nm, n = FLOAT_INTRINSIC[t]
            st0[0] = '%s(%s)' % (nm, ', '.join(V(espargs.get(4 * i)) for i in range(n)))
        elif t in DOUBLE_INTRINSIC and espargs is not None:
            st0[0] = '%s(%s)' % (DOUBLE_INTRINSIC[t], V(espargs.get(0)))
        elif t in VARIADIC:
            outp, argc, argv = P[-1], P[-2], P[-3]
            n = argc[1] if argc and argc[0] == 'imm' else 0
            args = []
            for i in range(n):
                p = ptrs.get(argv[1] + 4 * i) if argv and argv[0] == 'addr' else None
                args.append(V(('addr', p)) if isinstance(p, int) else (p[1] if isinstance(p, tuple) else '?'))
            e = '%s(%s)' % (VARIADIC[t], ', '.join(args))
            if outp and outp[0] == 'addr':
                slots[outp[1]] = e
        elif t == 0x4056D0:
            opname = cstr(P[-1][1]) if P and P[-1] and P[-1][0] == 'imm' else None
            if opname in ('++', '--') and len(P) >= 2:
                emit('%s%s' % (V(P[-2]), opname))
                incdec[0] = V(P[-2])
        elif t == F_DRAW_SPRITE_EXT:
            # cdecl (inst, sprite, subimg, x, y, xscale, yscale, rot : float, colour, alpha : float)
            raw = [espargs[k] for k in sorted(espargs)] if espargs else list(reversed(P))
            vals = []
            for i_, x_ in enumerate(raw):
                if i_ in (3, 4, 5, 6, 7, 9) and x_ and x_[0] == 'imm':
                    vals.append(fmt_num(round(struct.unpack('<f', struct.pack('<I', x_[1]))[0], 6)))
                elif i_ == 1 and x_ and x_[0] == 'imm' and x_[1] < len(assets['sprites']):
                    vals.append(assets['sprites'][x_[1]])
                else:
                    vals.append(V(x_))
            emit('draw_sprite_ext(%s)' % ', '.join(vals[1:]))
        elif t == 0x13F7220 and espargs is not None:
            def dbl(off):
                lo, hi = espargs.get(off), espargs.get(off + 4)
                if lo and hi and lo[0] == 'imm' and hi[0] == 'imm':
                    return pdouble(lo[1], hi[1])
                return V(lo)
            st0[0] = 'fmod(%s, %s)' % (dbl(0), dbl(8))
        elif t == F_RANDOM_RANGE and espargs is not None:
            st0[0] = '(%s + random(%s))' % (V(espargs.get(0)), V(espargs.get(8)))
        elif t == F_WITH_BEGIN:
            emit('with (%s) {  // iterator begin' % V(P[-4] if len(P) >= 4 else None))
            skip_jcc[0] = True
        elif t == F_WITH_NEXT:
            regs['eax'] = ('boolres', 'with_next')
        elif t == F_WITH_END:
            emit('}  // with end', comment='')
        elif t == F_INSTANCE_DESTROY:
            emit('instance_destroy(%s)' % ', '.join(V(x) for x in reversed(P[:-2])))
        elif t == F_INSTANCE_EXISTS:
            regs['eax'] = ('boolres', 'instance_exists(%s)' % V(P[-3] if len(P) >= 3 else None))
        elif t in lab and lab[t].startswith(('gml_Script_', 'gml_GlobalScript_')):
            argv, argc, ret = P[-5], P[-4], P[-3]
            n = argc[1] if argc and argc[0] == 'imm' else 0
            args = []
            for i in range(n):
                p = ptrs.get(argv[1] + 4 * i) if argv and argv[0] == 'addr' else None
                args.append(p[1] if isinstance(p, tuple) else (V(('addr', p)) if p is not None else '?'))
            e = '%s(%s)' % (lab[t].replace('gml_Script_', '').replace('gml_GlobalScript_', ''), ', '.join(args))
            if ret and ret[0] == 'addr':
                slots[ret[1]] = e
                ret_slots.add(ret[1])
            regs['eax'] = ret
            emit(e)
        elif t in lab and lab[t].startswith('F_'):
            emit('%s(%s)' % (lab[t][2:], ', '.join(V(x) for x in reversed(P))))
        else:
            cls = classify_op(t)
            if cls and espargs and not P:
                op_, ka, kb = cls
                outp = espargs.get(0)
                ea = V(espargs.get(4)) if ka != 'double' else V(espargs.get(4))
                eb = V(espargs.get(12 if ka == 'double' else 8))
                e = '(%s %s %s)' % (ea, op_, eb)
                if outp and outp[0] == 'addr':
                    slots[outp[1]] = e
                regs['eax'] = outp
            elif cls:
                op_, ka, kb = cls
                outp = P[-1]
                ea, pos = take_arg(P, -2, ka)
                eb, pos = take_arg(P, pos, kb)
                e = '(%s %s %s)' % (ea, op_, eb)
                if outp and outp[0] == 'addr':
                    slots[outp[1]] = e
                regs['eax'] = outp
            elif espargs:
                st0[0] = 'sub_%x(%s)' % (t, ', '.join(V(espargs[k]) for k in sorted(espargs)))
            else:
                emit('sub_%x(%s)' % (t, ', '.join(V(x) for x in reversed(P))))
    except (IndexError, TypeError, KeyError):
        emit('?call %x (unparsed)' % t)
    pushes = []
    espargs = None

flush_chain()
while blocks:
    blocks.pop()
    indent[0] -= 1
    out.append('    ' * max(indent[0], 0) + '}')

# ---- cosmetic clean-up (does not change meaning)
def cleanup(lines):
    changed = True
    while changed:
        changed = False
        res = []
        i = 0
        while i < len(lines):
            cur = lines[i].strip()
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ''
            nxt2 = lines[i + 2].strip() if i + 2 < len(lines) else ''
            # empty if / if-else bodies
            if cur.startswith('if (') and cur.split('  //')[0].endswith('{') and nxt == '}':
                i += 2; changed = True; continue
            if cur.startswith('if (') and cur.split('  //')[0].endswith('{') and nxt == '} else {' and nxt2 == '}':
                i += 3; changed = True; continue
            if cur == '} else {' and nxt == '}':
                res.append(lines[i + 1]); i += 2; changed = True; continue
            # "call(...)" followed by "x = ?" -> "x = call(...)" (result passed in a register)
            if nxt.split('  //')[0].endswith(' = ?') and cur.split('  //')[0].endswith(')') and ' = ' not in cur                     and not cur.startswith(('if ', '}', 'with ', 'loop')):
                lhs = lines[i + 1].split(' = ?')[0]
                res.append(lhs + ' = ' + cur.split('  //')[0] + ('  //' + nxt.split('  //')[1] if '  //' in nxt else ''))
                i += 2
                changed = True
                continue
            # "call(...)" immediately followed by "local_x = call(...)"
            if nxt.startswith('local_') and ' = ' in nxt and nxt.split(' = ', 1)[1].split('  //')[0] == cur.split('  //')[0]:
                i += 1; changed = True; continue
            res.append(lines[i]); i += 1
        lines = res
    return lines


print('// %s @ %x   (lifted by yyclift2; pseudo-GML, not compilable)' % (target, start))
for line in cleanup(out):
    print(line)
