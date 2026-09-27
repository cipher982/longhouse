/**
 * Hearth shaders: a Stam stable-fluids + combustion solver over a shared
 * tile atlas (one 32x44 tile per visible fire), a blackbody compositor with
 * a Voronoi coal bed, a GPU flame-height probe, and GPU sparks.
 *
 * Ported from the approved prototype
 * (misc-private/longhouse-hearth-2026-09/hearth.html). Changes from it: the
 * tile count is the atlas capacity, the compositor and sparks clip to each
 * row's visible rect (sticky headers, the scroll viewport), and the bed can
 * turn to ash for an ended session.
 */

export const TW = 32;
export const TH = 44;
/** Fuel sources per tile: the main root plus five flare slots. */
export const NS = 6;
export const PW = 64;
export const PH = 16;
export const NP = PW * PH;
export const SLOT_X = [0.5, 0.3, 0.7, 0.17, 0.83];

export function hearthShaders(nt: number) {
  const AW = TW * nt;
  const AH = TH;
  const HDR = `#version 300 es
precision highp float; precision highp int; precision highp sampler2D;
const int TWI=${TW}, THI=${TH}, NT=${nt}, NS=${NS}, PW=${PW};
const float TW=${TW}.0, TH=${TH}.0;
const vec2 ATLAS=vec2(${AW}.0, ${AH}.0);
ivec2 nb(ivec2 c,int dx,int dy){int x0=(c.x/TWI)*TWI;return ivec2(clamp(c.x+dx,x0,x0+TWI-1),clamp(c.y+dy,0,THI-1));}
vec2 clampTile(vec2 q,float x0){return vec2(clamp(q.x,x0+0.5,x0+TW-0.5),clamp(q.y,0.5,TH-0.5));}
float hash(vec3 p){p=fract(p*vec3(.1031,.1030,.0973));p+=dot(p,p.yxz+33.33);return fract((p.x+p.y)*p.z);}
`;
  const VS_FULL = `#version 300 es
void main(){vec2 p=vec2(float((gl_VertexID<<1)&2),float(gl_VertexID&2));gl_Position=vec4(p*2.0-1.0,0.0,1.0);}`;
  // RK2 backtrace + bilinear sample; walls clamp to the tile, the open top lets ambient in
  const FS_ADVECT = HDR + `
uniform sampler2D u_vel,u_src; uniform float u_dt,u_open; out vec4 o;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);vec2 p=gl_FragCoord.xy;float x0=floor(p.x/TW)*TW;
 vec2 v1=texelFetch(u_vel,c,0).xy; vec2 m=clampTile(p-0.5*u_dt*v1,x0);
 vec2 v2=texture(u_vel,m/ATLAS).xy; vec2 q=p-u_dt*v2;
 float over=clamp(q.y-(TH-0.5),0.0,1.0)+clamp(0.5-q.y,0.0,1.0);
 vec4 r=texture(u_src,clampTile(q,x0)/ATLAS);
 o=mix(r,vec4(0.0),over*u_open);}`;
  // MacCormack correction with a min/max limiter over the backtraced stencil
  const FS_MACC = HDR + `
uniform sampler2D u_vel,u_orig,u_fwd,u_bwd; uniform float u_dt; out vec4 o;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);vec2 p=gl_FragCoord.xy;float x0=floor(p.x/TW)*TW;int xi=int(x0);
 vec2 v1=texelFetch(u_vel,c,0).xy; vec2 m=clampTile(p-0.5*u_dt*v1,x0);
 vec2 v2=texture(u_vel,m/ATLAS).xy; vec2 q=clampTile(p-u_dt*v2,x0);
 vec4 fh=texelFetch(u_fwd,c,0), bt=texelFetch(u_bwd,c,0), og=texelFetch(u_orig,c,0);
 vec4 r=fh+0.5*(og-bt);
 ivec2 b=ivec2(floor(q-0.5));
 int xa=clamp(b.x,xi,xi+TWI-1), xb=clamp(b.x+1,xi,xi+TWI-1), ya=clamp(b.y,0,THI-1), yb=clamp(b.y+1,0,THI-1);
 vec4 s0=texelFetch(u_orig,ivec2(xa,ya),0),s1=texelFetch(u_orig,ivec2(xb,ya),0),s2=texelFetch(u_orig,ivec2(xa,yb),0),s3=texelFetch(u_orig,ivec2(xb,yb),0);
 vec4 mn=min(min(s0,s1),min(s2,s3)), mx=max(max(s0,s1),max(s2,s3));
 if(q.y>=TH-0.6) { mn=min(mn,fh); mx=max(mx,fh); }
 o=clamp(r,mn,mx);}`;
  const FS_CURL = HDR + `
uniform sampler2D u_vel; out vec4 o;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);
 float L=texelFetch(u_vel,nb(c,-1,0),0).y,R=texelFetch(u_vel,nb(c,1,0),0).y,B=texelFetch(u_vel,nb(c,0,-1),0).x,T=texelFetch(u_vel,nb(c,0,1),0).x;
 o=vec4(0.5*((R-L)-(T-B)),0.0,0.0,1.0);}`;
  // Boussinesq buoyancy + vorticity confinement + stoke gust
  const FS_FORCE = HDR + `
uniform sampler2D u_vel,u_curl,u_s; uniform float u_dt,u_time; uniform vec4 u_tile[NT]; out vec4 o;
const float BETA=24.0, KAPPA=0.3, TURB=24.0, NU=0.03;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);int t=c.x/TWI;vec4 tp=u_tile[t];
 vec2 v=texelFetch(u_vel,c,0).xy;
 vec2 vavg=0.25*(texelFetch(u_vel,nb(c,-1,0),0).xy+texelFetch(u_vel,nb(c,1,0),0).xy+texelFetch(u_vel,nb(c,0,-1),0).xy+texelFetch(u_vel,nb(c,0,1),0).xy);
 v=mix(v,vavg,NU);
 float wC=texelFetch(u_curl,c,0).x;
 float wL=abs(texelFetch(u_curl,nb(c,-1,0),0).x),wR=abs(texelFetch(u_curl,nb(c,1,0),0).x),wB=abs(texelFetch(u_curl,nb(c,0,-1),0).x),wT=abs(texelFetch(u_curl,nb(c,0,1),0).x);
 vec2 g=0.5*vec2(wR-wL,wT-wB); vec2 N=g/(length(g)+1e-5);
 vec2 f=tp.z*vec2(N.y*wC,-N.x*wC);
 vec4 s=texelFetch(u_s,c,0);
 f.y+=BETA*s.r-KAPPA*s.b;
 float lx=gl_FragCoord.x-float(t)*TW;
 float nx=sin(lx*0.55+u_time*3.1+float(t)*1.7)*sin(gl_FragCoord.y*0.31-u_time*4.3)+0.5*sin(gl_FragCoord.y*0.83+u_time*7.9+lx*0.2);
 f.x+=TURB*tp.w*nx*min(s.r,1.5);
 float y=gl_FragCoord.y/TH, x=(gl_FragCoord.x-float(t)*TW)/TW;
 f.y+=tp.y*(1.0-smoothstep(0.05,0.8,y))*(0.7+0.3*cos(6.2832*(x-0.5)));
 v+=f*u_dt; v*=0.996;
 o=vec4(v,0.0,1.0);}`;
  // combustion (Arrhenius), soot formation/oxidation, radiative loss, fuel + heat sources, bed heat
  const FS_REACT = HDR + `
uniform sampler2D u_s; uniform float u_dt,u_time; uniform vec4 u_tile[NT]; uniform vec4 u_src[NT*NS]; out vec4 o;
const float A=900.0, TA=6000.0, KMIX=2.0, THAD=1.55, Y0=0.05, YIELD=0.5, KOX=5.0, KC=2.6, KS=1.5, KY=1.0, KPYR=0.6;
float vnoise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);
 float a=hash(vec3(i,7.0)),b=hash(vec3(i+vec2(1,0),7.0)),c=hash(vec3(i+vec2(0,1),7.0)),d=hash(vec3(i+vec2(1,1),7.0));
 return mix(mix(a,b,f.x),mix(c,d,f.x),f.y);}
void main(){ivec2 c=ivec2(gl_FragCoord.xy);int t=c.x/TWI;vec2 lp=vec2(gl_FragCoord.x-float(t)*TW,gl_FragCoord.y);
 vec4 s=texelFetch(u_s,c,0); float th=max(s.r,0.0),Y=max(s.g,0.0),S=max(s.b,0.0);
 float T=300.0+1000.0*th;
 float Tig=max(T,mix(T,300.0+1000.0*u_tile[t].x,exp(-lp.y/3.5)));
 float kin=A*exp(-TA/Tig);
 float hs=clamp((u_src[t*NS].y-0.08)/0.08,0.0,1.0), mixs=mix(4.0,1.0,hs), kmix=KMIX*mixs;
 float k=1.0/(1.0/max(kin,1e-6)+1.0/kmix), f=1.0-exp(-k*u_dt);
 float burn=Y*f; Y-=burn; S+=YIELD*burn;
 th+=max(0.0,THAD-th)*f*Y/(Y+Y0)*3.0;
 float pyr=min(Y,KPYR*Y*smoothstep(0.6,1.1,th)*u_dt); Y-=pyr; S+=pyr*0.4/(1.0+S);
 S-=S*KOX*smoothstep(0.9,1.6,th)*smoothstep(0.5,0.0,Y)*u_dt;
 th*=exp(-KC*mixs*u_dt*(1.0+0.25*th*th*th));
 S*=exp(-KS*mixs*u_dt);
 Y*=exp(-KY*u_dt);
 float n1=vnoise(vec2(gl_FragCoord.x/3.0,u_time*6.0)), n2=0.5+0.5*vnoise(vec2(gl_FragCoord.x/2.0+9.0,u_time*9.0));
 float band=exp(-pow((lp.y-4.4)/1.9,2.0));
 for(int k=0;k<NS;k++){vec4 q=u_src[t*NS+k]; if(q.z<=0.0) continue;
   float dx=(lp.x/TW-q.x)/q.y; float g=exp(-dx*dx)*band;
   Y+=q.z*g*u_dt*(0.4+1.2*n1); th+=q.w*g*u_dt*(0.5+n2);}
 float bed=0.35*u_tile[t].x; float by=exp(-lp.y/0.8);
 th+=(bed*(0.6+0.8*n2)-th)*by*min(1.0,4.0*u_dt)*step(th,bed);
 o=vec4(th,Y,S,1.0);}`;
  const FS_DIV = HDR + `
uniform sampler2D u_vel; out vec4 o;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);int x0=(c.x/TWI)*TWI;vec2 C=texelFetch(u_vel,c,0).xy;
 vec2 L=c.x>x0?texelFetch(u_vel,c+ivec2(-1,0),0).xy:vec2(-C.x,C.y);
 vec2 R=c.x<x0+TWI-1?texelFetch(u_vel,c+ivec2(1,0),0).xy:vec2(-C.x,C.y);
 vec2 B=c.y>0?texelFetch(u_vel,c+ivec2(0,-1),0).xy:C;
 vec2 T=c.y<THI-1?texelFetch(u_vel,c+ivec2(0,1),0).xy:C;
 o=vec4(0.5*(R.x-L.x+T.y-B.y),0.0,0.0,1.0);}`;
  const FS_JACOBI = HDR + `
uniform sampler2D u_p,u_div; out vec4 o;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);int x0=(c.x/TWI)*TWI;float C=texelFetch(u_p,c,0).x;
 float L=c.x>x0?texelFetch(u_p,c+ivec2(-1,0),0).x:C;
 float R=c.x<x0+TWI-1?texelFetch(u_p,c+ivec2(1,0),0).x:C;
 float B=c.y>0?texelFetch(u_p,c+ivec2(0,-1),0).x:0.0;
 float T=c.y<THI-1?texelFetch(u_p,c+ivec2(0,1),0).x:0.0;
 o=vec4(0.25*(L+R+B+T-texelFetch(u_div,c,0).x),0.0,0.0,1.0);}`;
  const FS_PROJECT = HDR + `
uniform sampler2D u_p,u_vel; out vec4 o;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);int x0=(c.x/TWI)*TWI;float C=texelFetch(u_p,c,0).x;
 float L=c.x>x0?texelFetch(u_p,c+ivec2(-1,0),0).x:C;
 float R=c.x<x0+TWI-1?texelFetch(u_p,c+ivec2(1,0),0).x:C;
 float B=c.y>0?texelFetch(u_p,c+ivec2(0,-1),0).x:0.0;
 float T=c.y<THI-1?texelFetch(u_p,c+ivec2(0,1),0).x:0.0;
 vec2 v=texelFetch(u_vel,c,0).xy-0.5*vec2(R-L,T-B);
 if(c.x==x0) v.x=max(v.x,0.0); if(c.x==x0+TWI-1) v.x=min(v.x,0.0); if(c.y==0) v.y=max(v.y,0.0);
 o=vec4(v,0.0,1.0);}`;
  // sparks: state A=(x,y,vx,vy) atlas cells, B=(T kelvin, life s, radius, tile)
  const FS_PUPDATE = HDR + `
uniform sampler2D u_pa,u_pb,u_vel; uniform float u_dt;
layout(location=0) out vec4 oA; layout(location=1) out vec4 oB;
const float DRAG=0.9, GRAV=12.0, COOL=4.0e-10, BEDY=4.6;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);vec4 A=texelFetch(u_pa,c,0),B=texelFetch(u_pb,c,0);
 if(B.y<=0.0){oA=A;oB=vec4(0.0);return;}
 float x0=B.w*TW; vec2 p=A.xy,v=A.zw; float r=B.z;
 vec2 u=texture(u_vel,clampTile(p,x0)/ATLAS).xy;
 float cd=DRAG/(r*r);
 v=(v+u_dt*(cd*u+vec2(0.0,-GRAV)))/(1.0+u_dt*cd);
 p+=v*u_dt;
 if(p.x<x0+0.4){p.x=x0+0.4;v.x=abs(v.x)*0.4;} if(p.x>x0+TW-0.4){p.x=x0+TW-0.4;v.x=-abs(v.x)*0.4;}
 float life=B.y-u_dt;
 if(p.y<BEDY){p.y=BEDY;v.y=abs(v.y)*0.2;v.x*=0.5;life-=u_dt*3.0;}
 float T=B.x; T=pow(1.0/(T*T*T)+3.0*COOL/r*u_dt,-1.0/3.0);
 if(T<680.0||p.y>TH+2.0) life=0.0;
 oA=vec4(p,v); oB=vec4(T,life,r,B.w);}`;
  const EMIT = `
const float K_S=1.6, K_G=0.03, EXPO=8.0;
float emitL(vec4 s, vec4 B){ float th=max(s.r,0.0), soot=max(s.b,0.0); return pow(B.a,4.0)*(1.0-exp(-(K_S*soot+K_G*th)))*EXPO; }`;
  const BB_COMMON = `
uniform sampler2D u_lut;
vec4 bb(float T){return texture(u_lut,vec2(clamp((T-400.0)/3000.0,0.0,1.0)*(255.0/256.0)+0.5/256.0,0.5));}`;
  // One instanced quad per tile, placed on its row's cell; clip is the cell's visible rect.
  // Kill one tile's sparks, so a tile handed to another row starts clean.
  const FS_PKILL = HDR + `
uniform sampler2D u_pa,u_pb; uniform float u_kill;
layout(location=0) out vec4 oA; layout(location=1) out vec4 oB;
void main(){ivec2 c=ivec2(gl_FragCoord.xy);vec4 A=texelFetch(u_pa,c,0),B=texelFetch(u_pb,c,0);
 if(abs(B.w-u_kill)<0.5) B=vec4(0.0);
 oA=A; oB=B;}`;
  const VS_COMP = HDR + `
uniform vec4 u_rect[NT]; uniform vec2 u_canvas; out vec2 v_uv; flat out int v_tile;
void main(){int t=gl_InstanceID;vec2 k=vec2(float(gl_VertexID&1),float((gl_VertexID>>1)&1));vec4 r=u_rect[t];
 v_uv=k; v_tile=t; gl_Position=r.z>0.0?vec4((r.xy+k*r.zw)/u_canvas*2.0-1.0,0.0,1.0):vec4(2.0,2.0,2.0,1.0);}`;
  const FS_COMP = HDR + BB_COMMON + EMIT + `
uniform sampler2D u_s,u_vel; uniform vec4 u_bed[NT]; uniform vec4 u_clip[NT]; uniform float u_time;
in vec2 v_uv; flat in int v_tile; out vec4 o;
const float K_A=0.9, EXPO_BED=3.4, BEDH=7.0;
vec4 cubic(vec2 p){p-=0.5;vec2 i=floor(p),f=p-i;vec2 f2=f*f,f3=f2*f;
 vec2 w0=(1.0-3.0*f+3.0*f2-f3)/6.0,w1=(4.0-6.0*f2+3.0*f3)/6.0,w2=(1.0+3.0*f+3.0*f2-3.0*f3)/6.0,w3=f3/6.0;
 vec2 g0=w0+w1,g1=w2+w3; vec2 h0=i-0.5+w1/g0, h1=i+1.5+w3/g1;
 return g0.y*(g0.x*texture(u_s,vec2(h0.x,h0.y)/ATLAS)+g1.x*texture(u_s,vec2(h1.x,h0.y)/ATLAS))
       +g1.y*(g0.x*texture(u_s,vec2(h0.x,h1.y)/ATLAS)+g1.x*texture(u_s,vec2(h1.x,h1.y)/ATLAS));}
vec2 h2(vec2 p){p=vec2(dot(p,vec2(127.1,311.7)),dot(p,vec2(269.5,183.3)));return fract(sin(p)*43758.5453);}
vec3 voro(vec2 x,out vec2 rel){vec2 n=floor(x),f=fract(x);float d1=8.0,d2=8.0;float id=0.0;rel=vec2(0.0);
 for(int j=-1;j<=1;j++)for(int i=-1;i<=1;i++){vec2 g=vec2(i,j);vec2 r=g+h2(n+g)*0.8+0.1-f;float d=dot(r,r);
  if(d<d1){d2=d1;d1=d;id=h2(n+g+17.0).x;rel=-r;}else if(d<d2)d2=d;}
 return vec3(sqrt(d1),sqrt(d2),id);}
float vn(float x){float i=floor(x),f=fract(x);float a=fract(sin(i*91.3)*437.5),b=fract(sin((i+1.0)*91.3)*437.5);return mix(a,b,f*f*(3.0-2.0*f));}
void main(){int t=v_tile;vec4 cl=u_clip[t];
 if(gl_FragCoord.x<cl.x||gl_FragCoord.y<cl.y||gl_FragCoord.x>cl.z||gl_FragCoord.y>cl.w) discard;
 float x0=float(t)*TW;vec2 lp=v_uv*vec2(TW,TH);
 vec4 s=cubic(vec2(clamp(lp.x,1.5,TW-1.5)+x0,clamp(lp.y,1.5,TH-1.0)));
 float th=max(s.r,0.0),soot=max(s.b,0.0);
 float T=300.0+1000.0*th; vec4 B=bb(T);
 float topFade=1.0-smoothstep(TH*0.72,TH-0.5,lp.y);
 vec3 col=B.rgb*emitL(s,B)*topFade;
 float a=(1.0-exp(-K_A*soot))*(1.0-smoothstep(0.2,0.8,th))*0.8;
 col+=vec3(0.020,0.014,0.010)*a;
 vec4 bd=u_bed[t];
 float taper=smoothstep(0.0,5.0,lp.x)*smoothstep(0.0,5.0,TW-lp.x);
 float top=BEDH*(0.7+0.4*vn(lp.x*0.3+bd.z*13.0))*mix(0.5,1.0,taper);
 vec2 bq=vec2(lp.x*0.2,lp.y*0.3+lp.x*0.035)+bd.z*7.13;
 vec2 rel; vec3 vr=voro(bq,rel);
 float edge=vr.y-vr.x;
 float lump=smoothstep(0.07,0.2,edge)*(1.0-smoothstep(0.5,0.68,vr.x));
 float bm=max(1.0-smoothstep(top-0.6,top,lp.y), lump*(1.0-smoothstep(top+0.2,top+1.0,lp.y)));
 if(bm>0.0){
   float thb=min(texture(u_s,vec2(x0+clamp(lp.x,0.5,TW-0.5),4.0)/ATLAS).r,1.3);
   float vb=length(texture(u_vel,vec2(x0+clamp(lp.x,0.5,TW-0.5),2.0)/ATLAS).xy);
   float depth=clamp(1.0-lp.y/max(top,0.5),0.0,1.0);
   float lit=smoothstep(600.0,950.0,bd.x);
   float dome=sqrt(max(0.0,1.0-vr.x*vr.x*2.2));
   float rim=1.0-smoothstep(0.05,0.32,edge);
   float Tg=max(bd.x,bd.y)+60.0+180.0*thb+50.0*clamp(vb/12.0,0.0,1.0)*lit;
   float Tf=bd.x+200.0*thb+((vr.z-0.5)*150.0-160.0*dome-120.0*clamp(rel.y*2.2,-1.0,1.0)+60.0*clamp(vb/12.0,0.0,1.0))*lit-60.0*(1.0-depth);
   float Ts=mix(Tf,Tg,rim*0.8);
   float Tb=mix(Tg,Ts,lump);
   vec4 BB=bb(Tb);
   vec3 eb=BB.rgb*pow(BB.a,3.5)*EXPO_BED;
   vec3 flameLight=bb(300.0+1000.0*thb).rgb*clamp(thb*1.2,0.0,1.0);
   float up=clamp(0.3+0.5*dome+0.9*rel.y,0.0,1.2);
   vec3 alb=lump*(vec3(0.009,0.008,0.0075)*(0.4+0.9*up)+0.14*flameLight*up);
   // ash: a burnt-out bed's lumps go pale grey; the gaps stay dark
   vec3 ash=vec3(0.085,0.080,0.074)*(0.45+0.8*up)*(0.75+0.5*vr.z);
   alb=mix(alb,lump*ash+(1.0-lump)*vec3(0.012,0.011,0.010),bd.w);
   col=mix(col,eb+alb,bm); a=mix(a,1.0,bm);
 }
 col=1.0-exp(-col);
 col=pow(col,vec3(1.0/2.2));
 o=vec4(col,a);}`;
  // flame height probe: one fragment per tile reports the height below which
  // 96% of its luminous emission lies, plus the total
  const FS_MEASURE = HDR + BB_COMMON + EMIT + `
uniform sampler2D u_s; out vec4 o;
void main(){int t=int(gl_FragCoord.x);int x0=t*TWI;float rows[THI];float tot=0.0;
 for(int y=0;y<THI;y++){float r=0.0;
  if(y>=5) for(int x=1;x<TWI-1;x++){vec4 s=texelFetch(u_s,ivec2(x0+x,y),0);r+=min(emitL(s,bb(300.0+1000.0*max(s.r,0.0))),1.0);}
  rows[y]=r;tot+=r;}
 float h=0.0,c=0.0;
 if(tot>1.5){for(int y=0;y<THI;y++){c+=rows[y];if(c>=0.96*tot){h=(float(y)+1.0)/TH;break;}}}
 o=vec4(h,tot,0.0,1.0);}`;
  const VS_SPARK = HDR + BB_COMMON + `
uniform sampler2D u_pa,u_pb; uniform vec4 u_rect[NT]; uniform vec4 u_clip[NT]; uniform vec2 u_canvas; uniform int u_lines; uniform float u_dpr;
out vec3 v_col; out float v_a; flat out vec4 v_clip;
void main(){int id=u_lines==1?gl_VertexID>>1:gl_VertexID;int e=u_lines==1?(gl_VertexID&1):0;
 ivec2 tc=ivec2(id%PW,id/PW);vec4 A=texelFetch(u_pa,tc,0),B=texelFetch(u_pb,tc,0);
 v_clip=vec4(0.0);
 if(B.y<=0.0){gl_Position=vec4(2.0,2.0,2.0,1.0);gl_PointSize=0.0;v_col=vec3(0.0);v_a=0.0;return;}
 int t=int(B.w+0.5);vec4 r=u_rect[t]; v_clip=u_clip[t];
 vec2 p=A.xy-float(e)*A.zw*0.06;
 vec2 uv=vec2((p.x-float(t)*TW)/TW,p.y/TH);
 gl_Position=r.z>0.0?vec4((r.xy+uv*r.zw)/u_canvas*2.0-1.0,0.0,1.0):vec4(2.0,2.0,2.0,1.0);
 float sc=r.z/TW; gl_PointSize=clamp((0.35+B.z*0.45)*sc,1.5*u_dpr,4.0*u_dpr);
 vec4 k=bb(B.x); float fade=smoothstep(0.0,0.35,B.y);
 v_col=k.rgb*pow(k.a,2.2)*3.2*fade; v_a=e==1?0.0:1.0;}`;
  const FS_SPARK = `#version 300 es
precision highp float; precision highp int; uniform int u_lines; in vec3 v_col; in float v_a; flat in vec4 v_clip; out vec4 o;
void main(){
 if(gl_FragCoord.x<v_clip.x||gl_FragCoord.y<v_clip.y||gl_FragCoord.x>v_clip.z||gl_FragCoord.y>v_clip.w) discard;
 float f;
 if(u_lines==1){f=0.55*v_a;}else{float d=length(gl_PointCoord-0.5)*2.0;f=exp(-d*d*2.8)*(1.0-smoothstep(0.75,1.0,d));}
 vec3 c=1.0-exp(-v_col*f*1.6); o=vec4(pow(c,vec3(1.0/2.2)),0.0);}`;
  return {
    AW,
    AH,
    VS_FULL,
    FS_ADVECT,
    FS_MACC,
    FS_CURL,
    FS_FORCE,
    FS_REACT,
    FS_DIV,
    FS_JACOBI,
    FS_PROJECT,
    FS_PUPDATE,
    FS_PKILL,
    VS_COMP,
    FS_COMP,
    FS_MEASURE,
    VS_SPARK,
    FS_SPARK,
  };
}

// ---- blackbody LUT: Planck x CIE 1931 (Wyman-Sloan-Shirley multi-lobe fit) -> XYZ -> linear sRGB ----
function cmf(l: number): [number, number, number] {
  const g = (x: number, m: number, s1: number, s2: number) => {
    const t = (x - m) / (x < m ? s1 : s2);
    return Math.exp(-0.5 * t * t);
  };
  return [
    1.056 * g(l, 599.8, 37.9, 31.0) + 0.362 * g(l, 442.0, 16.0, 26.7) - 0.065 * g(l, 501.1, 20.4, 26.2),
    0.821 * g(l, 568.8, 46.9, 40.5) + 0.286 * g(l, 530.9, 16.3, 31.1),
    1.217 * g(l, 437.0, 11.8, 36.0) + 0.681 * g(l, 459.0, 26.0, 13.8),
  ];
}

function planckXYZ(T: number): [number, number, number] {
  let X = 0;
  let Y = 0;
  let Z = 0;
  for (let l = 380; l <= 780; l += 5) {
    const lm = l * 1e-9;
    const B = 1 / (Math.pow(lm, 5) * (Math.exp(1.4388e-2 / (lm * T)) - 1));
    const [x, y, z] = cmf(l);
    X += B * x;
    Y += B * y;
    Z += B * z;
  }
  return [X, Y, Z];
}

const BB_TMIN = 620;
const BB_TMAX = 2400;

/** RGBA per kelvin from 400 K to 3400 K: normalised chromaticity plus log-radiance brightness. */
export function buildBlackbodyLUT(n = 256): Float32Array {
  const out = new Float32Array(n * 4);
  const lnMin = Math.log(planckXYZ(BB_TMIN)[1]);
  const lnMax = Math.log(planckXYZ(BB_TMAX)[1]);
  for (let i = 0; i < n; i++) {
    const T = 400 + (3000 * i) / (n - 1);
    const [X, Y, Z] = planckXYZ(T);
    const r = Math.max(0, 3.2406 * X - 1.5372 * Y - 0.4986 * Z);
    const g = Math.max(0, -0.9689 * X + 1.8758 * Y + 0.0415 * Z);
    const b = Math.max(0, 0.0557 * X - 0.204 * Y + 1.057 * Z);
    const m = Math.max(r, g, b) || 1;
    out.set([r / m, g / m, b / m, Math.min(1, Math.max(0, (Math.log(Y) - lnMin) / (lnMax - lnMin)))], i * 4);
  }
  return out;
}
