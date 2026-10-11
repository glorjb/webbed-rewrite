import struct, sys, json
p = sys.argv[1]
d = open(p,'rb').read()
assert d[:4]==b'FORM'
def u32(o): return struct.unpack_from('<I',d,o)[0]
def s(ptr):
    if ptr==0: return None
    e=d.index(b'\0',ptr); return d[ptr:e].decode('utf8','replace')
chunks={}
o=8
while o < 8+u32(4):
    n=d[o:o+4].decode(); L=u32(o+4); chunks[n]=(o+8,L); o+=8+L
print("chunks:", ' '.join(f"{k}({v[1]})" for k,v in chunks.items()))
g,_=chunks['GEN8']
print("GEN8 debug=%d bytecode=%d"%(d[g],d[g+1]))
print("filename",s(u32(g+4)),"config",s(u32(g+8)))
print("gameID",u32(g+20),"name",s(u32(g+40)))
ma,mi,re,bu=struct.unpack_from('<4I',d,g+44); print("GEN8 version %d.%d.%d.%d"%(ma,mi,re,bu))
w,h,flags=struct.unpack_from('<3I',d,g+60); print("window",w,h,"flags",hex(flags))
ts=struct.unpack_from('<Q',d,g+92)[0]; import datetime; print("timestamp",datetime.datetime.fromtimestamp(ts,datetime.UTC))
print("displayname",s(u32(g+100)))
steam=u32(g+120); print("steamappid?",steam)
def plist(name):
    if name not in chunks: return []
    o,L=chunks[name]
    if L==0: return []
    n=u32(o); return [u32(o+4+4*i) for i in range(n)]
names={}
for c in ['SPRT','SOND','OBJT','ROOM','SCPT','BGND','PATH','FONT','SHDR','TMLN','SEQN','PSEM','PSYS','AUDO','TXTR','TPAG','EXTN','AGRP','TGIN','ACRV','FEAT','FEDS','PART']:
    ptrs=plist(c)
    nm=[]
    if c in ('SPRT','SOND','OBJT','ROOM','SCPT','BGND','PATH','FONT','SHDR','TMLN','PSEM','PSYS','AGRP','EXTN','SEQN','ACRV'):
        for p_ in ptrs:
            try:
                q=u32(p_) if c!='SEQN' else u32(p_+4)
                nm.append(s(q))
            except Exception: nm.append('?')
    names[c]=nm
    print(f"{c}: {len(ptrs)}")
# CODE entries
if 'CODE' in chunks:
    print("CODE:",len(plist('CODE')))
json.dump(names,open(sys.argv[2],'w'),indent=0)
