"""Crop the second batch of shop photos and place them in src.html.
usage: add_stills.py <uploads dir>
"""
import os, sys
from PIL import Image

here = os.path.dirname(os.path.abspath(__file__))
U = sys.argv[1]
# name: (upload id, crop box) -- crops also keep bystanders out of frame
CROPS = {
    'kia':     ('be072def', (0, 330, 1320, 1320)),
    'tech':    ('4b5ee622', (0, 200, 1320, 1400)),
    'm5rack':  ('0ec979f8', (0, 420, 1215, 1330)),
    'm5rear':  ('833da329', (0, 330, 1320, 1320)),
    's2klift': ('50c2f9e1', (0, 330, 1320, 1320)),
    'gtr':     ('ac6f82a1', None),
    'wrx':     ('4aed70ec', (130, 380, 1320, 1300)),
}
for name, (uid, box) in CROPS.items():
    im = Image.open(os.path.join(U, uid + '-image.jpg')).convert('RGB')
    if box:
        im = im.crop(box)
    im.thumbnail((900, 900))
    im.save(os.path.join(here, 'img', name + '.jpg'), quality=78)
    print(name, im.size)

p = os.path.join(here, 'src.html')
s = open(p, encoding='utf-8').read()

def rep(a, b):
    global s
    assert s.count(a) == 1, (a[:70], s.count(a))
    s = s.replace(a, b)

rep('<img data-img="racks15" alt="Lowered Nissan Silvia on the alignment rack">',
    '<img data-img="kia" alt="White sedan on the alignment rack in front of the FlyyAir logo wall">')
rep('<img data-img="rackm3" alt="" style="border-radius:16px', '<img data-img="kia" alt="" style="border-radius:16px')
rep('<section id="builds" style="padding-top:0">',
    '''<section style="padding-top:0"><div class="wrap">
  <div class="band reveal"><img data-img="tech" alt="Technician fitting a wheel at the FlyyAir shop">
    <div><span class="eyebrow">In-house</span><h3>We do the work ourselves.</h3>
    <p class="lead" style="font-size:15px">We spent years as installers for well-known brands before building our own. Every FlyyAir install, alignment and tune happens here in our Kissimmee shop.</p>
    <a class="btn" href="#/appointment">Book an install</a></div>
  </div>
</div></section>
<section id="builds" style="padding-top:0">''')
rep("['','CR-V','Aligned','crv']];",
    "['','CR-V','Aligned','crv'],['w','M5 Touring','On the alignment rack','m5rack'],['','GT-R','On the flush rack','gtr'],"
    "['','WRX','On the flush rack','wrx'],['','K5','On the alignment rack','kia'],['w','S2000','Coilover install','s2klift'],"
    "['','M5 Touring','On the lift','m5rear']];")
rep("const film=['wagon','m3gas','racks15','typer','mdx','rackm3','tlx','m3roof','m3rear','m3shop'];",
    "const film=['wagon','gtr','m3gas','racks15','m5rack','typer','wrx','mdx','rackm3','kia','tlx','m3roof','s2klift','m3rear'];")
open(p, 'w', encoding='utf-8').write(s)
print('placed')
