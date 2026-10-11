import struct,sys,json
d=open(sys.argv[1],'rb').read()
u32=lambda o:struct.unpack_from('<I',d,o)[0]; i32=lambda o:struct.unpack_from('<i',d,o)[0]; f32=lambda o:struct.unpack_from('<f',d,o)[0]
def s(p): e=d.index(b'\0',p); return d[p:e].decode()
ch={};o=8
while o<len(d):
    ch[d[o:o+4].decode()]=(o+8,u32(o+4)); o+=8+u32(o+4)
def plist(n):
    o,L=ch[n]; return [u32(o+4+4*i) for i in range(u32(o))]
spr=[s(u32(p)) for p in plist('SPRT')]
objs=[]
for p in plist('OBJT'):
    name=s(u32(p)); sp=i32(p+4)
    vis,solid,depth,pers,parent,mask,phys,sensor,shape=u32(p+8),u32(p+12),i32(p+16),u32(p+20),i32(p+24),i32(p+28),u32(p+32),u32(p+36),u32(p+40)
    dens,rest,grp,ld,ad,nv,fr,awake,kin=f32(p+44),f32(p+48),u32(p+52),f32(p+56),f32(p+60),i32(p+64),f32(p+68),u32(p+72),u32(p+76)
    objs.append(dict(name=name,sprite=spr[sp] if 0<=sp<len(spr) else None,parent=parent,depth=depth,solid=solid,physics=phys,sensor=sensor,shape=shape,density=dens,restitution=rest,group=grp,lin_damp=ld,ang_damp=ad,verts=nv,friction=fr,awake=awake,kinematic=kin))
names=[x['name'] for x in objs]
for x in objs: x['parent']=names[x['parent']] if 0<=x['parent']<len(names) else None
json.dump(objs,open(sys.argv[2],'w'),indent=1)
ph=[x for x in objs if x['physics']]
print("objects:",len(objs),"physics-enabled:",len(ph))
for x in objs:
    if any(k in x['name'].lower() for k in ['player','web','spider','rope','solid','ground']) and not x['name'].startswith('oSign'):
        print({k:(round(v,4) if isinstance(v,float) else v) for k,v in x.items()})
