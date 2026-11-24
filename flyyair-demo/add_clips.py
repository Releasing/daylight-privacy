"""Re-encode the generated clips for the web and wire them into src.html.
usage: add_clips.py <dir with clip1.mp4 .. clip6.mp4>
"""
import os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
src_dir = sys.argv[1]
os.makedirs(os.path.join(here, 'vid'), exist_ok=True)

# name: (source, video filter)
ENC = {
    'balancer': ('clip1', 'crop=834:960:0:0,scale=540:-2'),   # crop removes screenshot icons at the bottom
    'gtr':      ('clip2', 'scale=960:-2'),
    'install':  ('clip3', 'scale=640:-2'),
    's2krack':  ('clip4', 'scale=540:-2'),
    'sensor':   ('clip5', 'scale=540:-2'),
    'coilover': ('clip6', 'scale=540:-2'),
}
for name, (clip, vf) in ENC.items():
    out = os.path.join(here, 'vid', name + '.mp4')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', os.path.join(src_dir, clip + '.mp4'), '-an', '-vf', vf,
                    '-c:v', 'libx264', '-crf', '29', '-preset', 'slow', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out], check=True)
    poster = os.path.join(here, 'img', 'v_' + name + '.jpg')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', out, '-frames:v', '1', '-q:v', '5', poster], check=True)
    print(name, os.path.getsize(out) // 1024, 'KB')

p = os.path.join(here, 'src.html')
s = open(p, encoding='utf-8').read()

def rep(a, b):
    global s
    assert s.count(a) == 1, (a[:70], s.count(a))
    s = s.replace(a, b)

V = lambda n, extra='': f'<video data-vid="{n}" data-img="v_{n}" muted loop playsinline preload="none" {extra}></video>'

rep('<a class="door reveal" href="#/performance"><img data-img="racks15" alt="">', '<a class="door reveal" href="#/performance">' + V('gtr'))
rep('<img class="reveal" data-img="rackm3" alt="BMW M3 on the FlyyAir in-ground alignment rack">', V('s2krack', 'class="reveal" aria-label="Honda S2000 on the alignment rack"'))
rep('<img data-img="m3shop" alt="Purple M3 in the FlyyAir shop" style="max-height:460px;object-fit:cover">', V('install', 'aria-label="BMW wagon on the lift during an install"'))
rep('<div class="band reveal" style="margin-top:18px"><img data-img="racks15"',
    '''<div class="bay reveal">
      <figure>''' + V('sensor') + '''<figcaption>Alignment heads on</figcaption></figure>
      <figure>''' + V('coilover') + '''<figcaption>Coilovers and springs</figcaption></figure>
      <figure>''' + V('balancer') + '''<figcaption>In the bay</figcaption></figure>
    </div>
    <div class="band reveal" style="margin-top:18px"><img data-img="racks15"''')

rep('</style>', '''
/* ---------- video ---------- */
video{display:block;max-width:100%;background:#000}
.door video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.62;transition:opacity .3s}
.door:hover video{opacity:.85}
.door>video{z-index:0!important}
.phead video{border-radius:20px;border:1px solid var(--line);width:100%;max-height:520px;object-fit:cover}
.bay{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:18px}
.bay figure{position:relative;border-radius:18px;overflow:hidden;border:1px solid var(--line);aspect-ratio:3/4}
.bay video{width:100%;height:100%;object-fit:cover}
.bay figcaption{position:absolute;left:0;right:0;bottom:0;padding:26px 14px 12px;background:linear-gradient(transparent,#000c);font-family:var(--display);font-size:20px;text-transform:uppercase;letter-spacing:.02em}
@media(max-width:700px){.bay{grid-template-columns:1fr 1fr}.bay figure:last-child{display:none}}
</style>''')
rep('.door>*:not(img){position:relative;z-index:1}', '.door>*:not(img):not(video){position:relative;z-index:1}')

rep("$$('[data-img]').forEach(e=>e.tagName==='image'?e.setAttribute('href',IMG[e.dataset.img]):e.src=IMG[e.dataset.img]);",
    "$$('[data-img]').forEach(e=>e.tagName==='image'?e.setAttribute('href',IMG[e.dataset.img]):e.tagName==='VIDEO'?e.poster=IMG[e.dataset.img]:e.src=IMG[e.dataset.img]);\n"
    "const VID=/*VIDEOS*/;\n"
    "const vio=new IntersectionObserver(es=>es.forEach(e=>{const v=e.target; if(e.isIntersecting&&!reduce){if(!v.src)v.src=VID[v.dataset.vid]; v.play().catch(()=>{})} else v.pause()}),{threshold:.2});\n"
    "$$('video[data-vid]').forEach(v=>vio.observe(v));")
open(p, 'w', encoding='utf-8').write(s)

b = open(os.path.join(here, 'build.py')).read()
old = "assert '/*IMAGES*/' in s"
assert old in b
b = b.replace(old, "vids={}\nvd=os.path.join(here,'vid')\nfor f in sorted(os.listdir(vd)) if os.path.isdir(vd) else []:\n"
                   "    vids[os.path.splitext(f)[0]]='data:video/mp4;base64,'+base64.b64encode(open(os.path.join(vd,f),'rb').read()).decode()\n"
                   "s=s.replace('/*VIDEOS*/',json.dumps(vids))\n" + old)
open(os.path.join(here, 'build.py'), 'w').write(b)
print('wired')
