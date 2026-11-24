# Inlines img/* into src.html as data URIs and writes index.html (single self-contained file).
import base64, json, os
here=os.path.dirname(os.path.abspath(__file__))
imgs={}
for f in sorted(os.listdir(os.path.join(here,'img'))):
    n,ext=os.path.splitext(f); mime='image/png' if ext=='.png' else 'image/jpeg'
    imgs[n]=f'data:{mime};base64,'+base64.b64encode(open(os.path.join(here,'img',f),'rb').read()).decode()
s=open(os.path.join(here,'src.html'),encoding='utf-8').read()
assert '/*IMAGES*/' in s
open(os.path.join(here,'index.html'),'w',encoding='utf-8').write(s.replace('/*IMAGES*/',json.dumps(imgs)))
print('built',len(imgs),'images')
