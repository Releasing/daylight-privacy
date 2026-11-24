# Crops product photos and the logo out of the owner-site screenshots supplied for the pitch.
import sys
from PIL import Image, ImageOps
U=sys.argv[1]
S={'68':'ac029a91','67':'8a2de3c8','66':'a0e4dc66','65':'6d09ecbc','64':'1bf1e26a','63':'9f6c5fa7','62':'0f9178e0','61':'9c97690e','60':'a6b48945','58':'860a46a9','59':'bd3fc372','57':'c60e9add','56':'f812ac9c','55':'1e02057e','54':'5f2b1390','53':'5d47c12a'}
C={ # name: (shot, x0,y0,x1,y1) in 921-wide display coords
'm3':('56',96,1170,396,1456),'shirt':('56',475,1124,875,1389),'tee':('57',113,696,379,1096),'alp4':('57',475,696,875,1096),
'links':('58',96,285,396,684),'frame':('58',475,334,875,635),'fresh':('58',96,925,396,1324),'key':('58',525,925,825,1324),
'magnet':('59',96,551,396,950),'manifold':('59',514,551,836,950),'tophat':('60',96,127,396,527),'strut':('60',475,127,875,527),
'rear':('60',46,829,446,1228),'bag':('60',475,896,875,1161),'line':('61',46,453,446,717),'shock':('61',475,453,875,717),
'bearing':('61',46,962,446,1361),'ring':('61',475,1029,875,1296),'bc':('62',46,861,446,1261),'install':('62',525,861,825,1261),
'mgmt':('63',46,388,446,720),'logoV':('55',262,196,676,412),'logoH':('56',44,140,712,286)}
for n,(s,x0,y0,x1,y1) in C.items():
    im=Image.open(f'{U}/{S[s]}-image.png').convert('RGB'); k=im.width/921
    c=im.crop(tuple(int(v*k) for v in (x0,y0,x1,y1)))
    if n=='logoH': c=ImageOps.invert(c)
    if n.startswith('logo'): c.save(f'img/{n}.png',optimize=True)
    else:
        c.thumbnail((440,440)); c.save(f'img/{n}.jpg',quality=80)
    print(n,c.size)
# Second batch (rack, CR-V, red car, struts) was cropped the same way from the later screenshots.

def logo_alpha(path):
    """Turn a white-on-black logo crop into white with a transparent background."""
    from PIL import Image
    l=Image.open(path).convert('L').point(lambda v:max(0,min(255,int((v-40)*255/150))))
    out=Image.new('RGBA',l.size,(255,255,255,0)); out.putalpha(l); out.save(path,optimize=True)
