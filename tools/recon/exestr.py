import re,sys,collections
d=open(sys.argv[1],'rb').read()
strs=[m.group().decode() for m in re.finditer(rb'[\x20-\x7e]{6,}',d)]
gml=[x for x in strs if x.startswith('gml_')]
print("gml_ strings:",len(gml))
c=collections.Counter(x.split('_')[1] for x in gml); print(c.most_common(6))
for pat in [r'2\.3\.\d+\.\d+',r'2\.\d\.\d+\.\d+',r'YYC|YoYo Compiler|yyc',r'Runner|runner\.']:
    hits=sorted(set(x for x in strs if re.search(pat,x)))[:8]; print(pat,'->',hits)
kw=re.compile(r'player|spider|web|silk|rope|thread|swing|jump|grav|crawl',re.I)
open(sys.argv[2],'w').write('\n'.join(sorted(set(x for x in gml))))
print([x for x in sorted(set(gml)) if kw.search(x)][:200])
