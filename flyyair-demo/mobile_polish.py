"""Phone-layout polish for src.html (one-off patch, kept for the record)."""
import os

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src.html')
s = open(p, encoding='utf-8').read()

def rep(a, b):
    global s
    assert s.count(a) == 1, (a[:70], s.count(a))
    s = s.replace(a, b)

CSS = '''
/* ---------- phone polish ---------- */
@media(max-width:760px){
  section{padding:56px 0}
  .page>section:first-child:not(#hero){padding-top:104px}
  footer{padding:40px 0 24px}
  .wrap{padding:0 16px}
  .hero-title{margin-top:66px;font-size:15.2vw}
  .stage{-webkit-mask-image:linear-gradient(transparent,#000 10%,#000 90%,transparent);mask-image:linear-gradient(transparent,#000 10%,#000 90%,transparent);width:100%}
  .hero-copy .eyebrow{letter-spacing:.1em;font-size:11px}
  .hero-bar{padding:14px 16px 28px}
  .hero-copy .row .btn{flex:1;justify-content:center}
  .remote{width:min(300px,84vw)}
  .r-keys button,.r-mid{height:46px}
  .doors{gap:10px}.door{min-height:210px;padding:18px}
  .door:after{background:linear-gradient(#0003 0%,#000e 78%)}
  .door h3{font-size:30px}
  .prod button{padding:10px 16px;font-size:13px}
  .prod .row{flex-wrap:wrap}
  .prod h4{font-size:13px}
  .chip{padding:11px 15px}
  .sw{width:44px;height:44px}
  .bay figcaption{font-size:15px;padding:22px 10px 10px}
  .camber{padding:18px}
  .camber input[type=range]{height:34px}
  .builds{grid-auto-flow:dense;grid-auto-rows:132px;gap:8px}
  .tile{padding:10px}.tile b{font-size:18px}.tile small{font-size:11px}
  .tile.t{grid-row:span 2}
  .band div{padding:22px}
  .band img{min-height:220px;max-height:300px}
  .info a{display:inline-block;padding:8px 0}
  .flinks{gap:6px 18px}.flinks a{padding:8px 0}
  .faq summary{font-size:16px}
  #bookCard{position:static!important}
  .tabs{display:flex;width:100%}.tabs button{flex:1;padding:11px 8px}
  .stats{gap:8px}.stat b{font-size:26px}
  .big{margin-bottom:18px}
  h2{font-size:clamp(36px,11vw,56px)}
  .lead{font-size:16px}
  .toast{bottom:76px;white-space:nowrap}
}
@media(max-width:380px){.count div{width:62px;font-size:22px}.bundle b{font-size:28px}}
</style>'''
rep('</style>', CSS)

# bigger car on phones: tighter crop of the same photo
rep("render(); requestAnimationFrame(tick);",
    "const heroSvg=$('.stage svg'), mq=matchMedia('(max-width:760px)');\n"
    "const fitHero=()=>heroSvg.setAttribute('viewBox',mq.matches?'78 446 1074 436':'0 352 1320 548');\n"
    "fitHero(); mq.addEventListener('change',fitHero);\n"
    "render(); requestAnimationFrame(tick);")

# after picking a service on a phone, bring the booking form into view
rep("$('#svc').onclick=e=>{const b=e.target.closest('[data-svc]');if(b){svcSel=+b.dataset.svc;drawSvc()}};",
    "$('#svc').onclick=e=>{const b=e.target.closest('[data-svc]');if(b){svcSel=+b.dataset.svc;drawSvc();"
    "if(matchMedia('(max-width:860px)').matches)$('#bookCard').scrollIntoView({behavior:reduce?'auto':'smooth',block:'start'})}};")
rep('#bookCard" style="align-self:start;position:sticky;top:80px"', '#bookCard" style="align-self:start;position:sticky;top:80px;scroll-margin-top:76px"') if '#bookCard" style=' in s else None
open(p, 'w', encoding='utf-8').write(s)
print('ok')
