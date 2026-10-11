import struct,sys,collections,json
d=open(sys.argv[1],'rb').read()
u32=lambda o:struct.unpack_from('<I',d,o)[0]; f32=lambda o:struct.unpack_from('<f',d,o)[0]
def s(p): e=d.index(b'\0',p); return d[p:e].decode()
ch={};o=8
while o<len(d):
    ch[d[o:o+4].decode()]=(o+8,u32(o+4)); o+=8+u32(o+4)
def plist(n):
    o,L=ch[n]; return [u32(o+4+4*i) for i in range(u32(o))]
c=collections.Counter(); rows=[]
for p in plist('ROOM'):
    r=dict(name=s(u32(p)),w=u32(p+8),h=u32(p+12),speed=u32(p+16),world=u32(p+56),gx=round(f32(p+76),4),gy=round(f32(p+80),4),mpp=round(f32(p+84),6))
    rows.append(r); c[(r['speed'],r['world'],r['gx'],r['gy'],r['mpp'])]+=1
print("room settings (speed, physicsWorld, gravX, gravY, metersPerPixel) -> count"); [print(k,v) for k,v in c.most_common()]
print("sample:",[r for r in rows if 'swing' in r['name'] or 'webBuilding' in r['name']][:4])
# sprites
spr=[]
for p in plist('SPRT'):
    spr.append(dict(name=s(u32(p)),w=u32(p+4),h=u32(p+8),ox=struct.unpack_from('<i',d,p+48)[0],oy=struct.unpack_from('<i',d,p+52)[0]))
json.dump(spr,open(sys.argv[2]+'/sprites.json','w'),indent=0)
snd=[s(u32(p)) for p in plist('SOND')]
json.dump(snd,open(sys.argv[2]+'/sounds.json','w'),indent=0)
import re
kw=re.compile(r'spider|web|silk|player|jump|land|walk|crawl|swing|snap|shoot|step',re.I)
print("player/web sprites:",[x['name'] for x in spr if kw.search(x['name'])][:80])
print("player/web sounds:",[x for x in snd if kw.search(x)])
print("audiogroups:",[s(u32(p)) for p in plist('AGRP')], "embedded AUDO:",len(plist('AUDO')))
