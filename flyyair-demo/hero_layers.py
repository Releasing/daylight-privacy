import cv2, numpy as np, json
im=cv2.imread('/root/.claude/uploads/a29a8351-5833-5854-8c2e-5c2af2df0839/2b1749bf-image.jpg'); H,W=im.shape[:2]
gray=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY); hsv=cv2.cvtColor(im,cv2.COLOR_BGR2HSV)
S=cv2.imread('gc_mask.png',0)
# remove wall panel / extinguisher that grabcut kept
box=np.zeros_like(S); box[500:615,262:425]=1; S[(box==1)&(gray<170)]=0
S[:645,1186:]=0
# add front bumper (bright pixels near the nose)
nb=np.zeros_like(S); nb[650:832,100:215]=1; S[(nb==1)&(gray>150)]=255
def arch(cx,cy,rx,ry,rim,ground=832):
    yy,xx=np.mgrid[0:H,0:W]; circ=(((xx-cx)/rx)**2+((yy-cy)/ry)**2<1)&(yy<=ground)
    dark=((gray<95)&circ).astype(np.uint8)*255
    dark=cv2.morphologyEx(dark,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
    n,lab,st,_=cv2.connectedComponentsWithStats(dark); big=1+np.argmax(st[1:,4])
    bm=(lab==big).astype(np.uint8); cv2.ellipse(bm,rim[0],rim[1],0,0,360,1,-1)
    pts=cv2.findNonZero(bm); hull=cv2.convexHull(pts)
    m=np.zeros_like(S); cv2.fillConvexPoly(m,hull,255); return m
Cf=arch(270,765,96,80,((270,760),(71,67))); Cr=arch(973,764,90,76,((976,756),(73,68))); C=cv2.bitwise_or(Cf,Cr)
S=cv2.morphologyEx(S,cv2.MORPH_CLOSE,np.ones((9,9),np.uint8)); S=cv2.morphologyEx(S,cv2.MORPH_OPEN,np.ones((5,5),np.uint8))
S[C>0]=0
n,lab,st,_=cv2.connectedComponentsWithStats(S); big=1+np.argmax(st[1:,4]); S=(lab==big).astype(np.uint8)*255
# fill holes
ff=S.copy(); cv2.floodFill(ff,np.zeros((H+2,W+2),np.uint8),(0,0),255); S=S|cv2.bitwise_not(ff); S[C>0]=0
S=cv2.GaussianBlur(S,(5,5),0); S=(S>127).astype(np.uint8)*255
def path(m,eps=0.7):
    cs,_=cv2.findContours(m,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE); d=''
    for c in cs:
        if cv2.contourArea(c)<200: continue
        a=cv2.approxPolyDP(c,eps,True)[:,0,:]
        d+='M'+' '.join(f'{x} {y}' for x,y in a)+'Z'
    return d
P={'body':path(S),'wheels':path(C),'under':path(cv2.erode(S|C,np.ones((5,5),np.uint8)),1.2)}
json.dump(P,open('paths.json','w')); print({k:len(v) for k,v in P.items()})
# preview composite at lift L
def comp(L):
    out=im.copy(); U=cv2.erode(S|C,np.ones((5,5),np.uint8)); out[U>0]=(14,13,12)
    out[C>0]=im[C>0]
    M=np.float32([[1,0,0],[0,1,-L]]); bs=cv2.warpAffine(S,M,(W,H)); bi=cv2.warpAffine(im,M,(W,H)); out[bs>0]=bi[bs>0]; return out
cv2.imwrite('comp.png',np.vstack([comp(22)[470:880,60:1260],cv2.resize(np.hstack([comp(22)[640:850,150:400],comp(22)[640:850,850:1100]]),None,fx=2.4,fy=2.4)]))
print('diff at 0:',int(np.abs(comp(0).astype(int)-im).sum()))
