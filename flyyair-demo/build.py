# Inlines img/* into src.html as data URIs and writes index.html (single self-contained file).
import base64, json, os
here=os.path.dirname(os.path.abspath(__file__))
imgs={}
s=open(os.path.join(here,'src.html'),encoding='utf-8').read()
for f in sorted(os.listdir(os.path.join(here,'img'))):
    n,ext=os.path.splitext(f)
    if ('"'+n+'"') not in s and ("'"+n+"'") not in s: continue
    mime='image/png' if ext=='.png' else 'image/jpeg'
    imgs[n]=f'data:{mime};base64,'+base64.b64encode(open(os.path.join(here,'img',f),'rb').read()).decode()
vids={}
vd=os.path.join(here,'vid')
for f in sorted(os.listdir(vd)) if os.path.isdir(vd) else []:
    vids[os.path.splitext(f)[0]]='data:video/mp4;base64,'+base64.b64encode(open(os.path.join(vd,f),'rb').read()).decode()
s=s.replace('/*VIDEOS*/',json.dumps(vids))
assert '/*IMAGES*/' in s
open(os.path.join(here,'index.html'),'w',encoding='utf-8').write(s.replace('/*IMAGES*/',json.dumps(imgs)))
print('built',len(imgs),'images')
