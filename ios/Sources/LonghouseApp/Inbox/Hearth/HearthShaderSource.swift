/// Metal source for the Hearth solver and compositor, compiled at runtime
/// with `MTLDevice.makeLibrary(source:options:)`.
///
/// A port of web/src/shared/instruments/hearth/shaders.ts: the same Stam
/// stable-fluids + combustion solver over a shared tile atlas (one 32x44 tile
/// per visible fire), the same blackbody compositor, Voronoi coal bed, light
/// spill, flame-height probe and sparks. The constants match the web; change
/// them together. Differences: compute kernels instead of fragment passes,
/// the 20 Jacobi iterations run in one threadgroup-memory dispatch per tile,
/// and each row's layer draws only its own tile.
///
/// Runtime compilation keeps the build free of the separately downloaded
/// Metal toolchain (Xcode 26+), which neither CI nor the bench installs.
enum HearthShaderSource {
    static let metal = #"""
#include <metal_stdlib>
using namespace metal;

constant int TWI = 32;
constant int THI = 44;
constant float TW = 32.0;
constant float TH = 44.0;
constant int CAP = 12;
constant int NS = 6;
constant int NP = 1024;
constant int CELLS = 1408;
constant int JACOBI = 20;
constant uint PRESSURE_THREADS = 256;

// tiles[t]  = ((Ts - 300) / 1000, gust, vorticity eps, turbulence)
// sources[t * NS + k] = (x, width, fuel rate, heat rate)
// flags[t]  = (clear this tile now, -, -, -)
struct HearthSim {
    float4 tiles[CAP];
    float4 sources[CAP * NS];
    float4 flags[CAP];
};

struct HearthPass {
    float dt;
    float open;
    float time;
    uint lo;
};

struct HearthSpark {
    float4 a; // x, y (atlas cells), vx, vy
    float4 b; // T (K), life (s), radius (cells), tile
};

struct HearthSpawn {
    HearthSpark spark;
    uint4 slot;
};

constexpr sampler cells(coord::pixel, address::clamp_to_edge, filter::linear);
constexpr sampler lutSampler(coord::normalized, address::clamp_to_edge, filter::linear);

static int2 nb(int2 c, int dx, int dy) {
    int x0 = (c.x / TWI) * TWI;
    return int2(clamp(c.x + dx, x0, x0 + TWI - 1), clamp(c.y + dy, 0, THI - 1));
}

static float2 clampTile(float2 q, float x0) {
    return float2(clamp(q.x, x0 + 0.5, x0 + TW - 0.5), clamp(q.y, 0.5, TH - 0.5));
}

static float hash3(float3 p) {
    p = fract(p * float3(0.1031, 0.1030, 0.0973));
    p += dot(p, p.yxz + 33.33);
    return fract((p.x + p.y) * p.z);
}

static float4 bb(texture2d<float> lut, float T) {
    return lut.sample(lutSampler, float2(clamp((T - 400.0) / 3000.0, 0.0, 1.0) * (255.0 / 256.0) + 0.5 / 256.0, 0.5));
}

static float emitL(float4 s, float4 B) {
    float th = max(s.r, 0.0), soot = max(s.b, 0.0);
    return pow(B.a, 4.0) * (1.0 - exp(-(1.6 * soot + 0.03 * th))) * 8.0;
}

static int2 cellOf(uint2 gid, constant HearthPass& pass) {
    return int2(int(pass.lo) * TWI + int(gid.x), int(gid.y));
}

// ---------------- solver ----------------

kernel void hearthClear(
    constant HearthSim& sim [[buffer(0)]],
    texture2d<float, access::write> t0 [[texture(0)]],
    texture2d<float, access::write> t1 [[texture(1)]],
    texture2d<float, access::write> t2 [[texture(2)]],
    texture2d<float, access::write> t3 [[texture(3)]],
    texture2d<float, access::write> t4 [[texture(4)]],
    texture2d<float, access::write> t5 [[texture(5)]],
    texture2d<float, access::write> t6 [[texture(6)]],
    texture2d<float, access::write> t7 [[texture(7)]],
    texture2d<float, access::write> t8 [[texture(8)]],
    texture2d<float, access::write> t9 [[texture(9)]],
    uint2 gid [[thread_position_in_grid]]) {
    if (sim.flags[gid.x / uint(TWI)].x <= 0.0) return;
    float4 z = float4(0.0);
    t0.write(z, gid); t1.write(z, gid); t2.write(z, gid); t3.write(z, gid); t4.write(z, gid);
    t5.write(z, gid); t6.write(z, gid); t7.write(z, gid); t8.write(z, gid); t9.write(z, gid);
}

// RK2 backtrace + bilinear sample; walls clamp to the tile, the open top lets ambient in
kernel void hearthAdvect(
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> vel [[texture(0)]],
    texture2d<float> src [[texture(1)]],
    texture2d<float, access::write> o [[texture(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    int2 c = cellOf(gid, pass);
    float2 p = float2(c) + 0.5;
    float x0 = floor(p.x / TW) * TW;
    float2 v1 = vel.read(uint2(c)).xy;
    float2 m = clampTile(p - 0.5 * pass.dt * v1, x0);
    float2 v2 = vel.sample(cells, m).xy;
    float2 q = p - pass.dt * v2;
    float over = clamp(q.y - (TH - 0.5), 0.0, 1.0) + clamp(0.5 - q.y, 0.0, 1.0);
    float4 r = src.sample(cells, clampTile(q, x0));
    o.write(mix(r, float4(0.0), over * pass.open), uint2(c));
}

// MacCormack correction with a min/max limiter over the backtraced stencil
kernel void hearthMacCormack(
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> vel [[texture(0)]],
    texture2d<float> orig [[texture(1)]],
    texture2d<float> fwd [[texture(2)]],
    texture2d<float> bwd [[texture(3)]],
    texture2d<float, access::write> o [[texture(4)]],
    uint2 gid [[thread_position_in_grid]]) {
    int2 c = cellOf(gid, pass);
    float2 p = float2(c) + 0.5;
    float x0 = floor(p.x / TW) * TW;
    int xi = int(x0);
    float2 v1 = vel.read(uint2(c)).xy;
    float2 m = clampTile(p - 0.5 * pass.dt * v1, x0);
    float2 v2 = vel.sample(cells, m).xy;
    float2 q = clampTile(p - pass.dt * v2, x0);
    float4 fh = fwd.read(uint2(c)), bt = bwd.read(uint2(c)), og = orig.read(uint2(c));
    float4 r = fh + 0.5 * (og - bt);
    int2 b = int2(floor(q - 0.5));
    int xa = clamp(b.x, xi, xi + TWI - 1), xb = clamp(b.x + 1, xi, xi + TWI - 1);
    int ya = clamp(b.y, 0, THI - 1), yb = clamp(b.y + 1, 0, THI - 1);
    float4 s0 = orig.read(uint2(xa, ya)), s1 = orig.read(uint2(xb, ya));
    float4 s2 = orig.read(uint2(xa, yb)), s3 = orig.read(uint2(xb, yb));
    float4 mn = min(min(s0, s1), min(s2, s3)), mx = max(max(s0, s1), max(s2, s3));
    if (q.y >= TH - 0.6) { mn = min(mn, fh); mx = max(mx, fh); }
    o.write(clamp(r, mn, mx), uint2(c));
}

kernel void hearthCurl(
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> vel [[texture(0)]],
    texture2d<float, access::write> o [[texture(1)]],
    uint2 gid [[thread_position_in_grid]]) {
    int2 c = cellOf(gid, pass);
    float L = vel.read(uint2(nb(c, -1, 0))).y, R = vel.read(uint2(nb(c, 1, 0))).y;
    float B = vel.read(uint2(nb(c, 0, -1))).x, T = vel.read(uint2(nb(c, 0, 1))).x;
    o.write(float4(0.5 * ((R - L) - (T - B)), 0.0, 0.0, 1.0), uint2(c));
}

// Boussinesq buoyancy + vorticity confinement + stoke gust
kernel void hearthForce(
    constant HearthSim& sim [[buffer(0)]],
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> vel [[texture(0)]],
    texture2d<float> curl [[texture(1)]],
    texture2d<float> scalar [[texture(2)]],
    texture2d<float, access::write> o [[texture(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    const float BETA = 24.0, KAPPA = 0.3, TURB = 24.0, NU = 0.03;
    int2 c = cellOf(gid, pass);
    int t = c.x / TWI;
    float4 tp = sim.tiles[t];
    float2 fc = float2(c) + 0.5;
    float2 v = vel.read(uint2(c)).xy;
    float2 vavg = 0.25 * (vel.read(uint2(nb(c, -1, 0))).xy + vel.read(uint2(nb(c, 1, 0))).xy
                        + vel.read(uint2(nb(c, 0, -1))).xy + vel.read(uint2(nb(c, 0, 1))).xy);
    v = mix(v, vavg, NU);
    float wC = curl.read(uint2(c)).x;
    float wL = abs(curl.read(uint2(nb(c, -1, 0))).x), wR = abs(curl.read(uint2(nb(c, 1, 0))).x);
    float wB = abs(curl.read(uint2(nb(c, 0, -1))).x), wT = abs(curl.read(uint2(nb(c, 0, 1))).x);
    float2 g = 0.5 * float2(wR - wL, wT - wB);
    float2 N = g / (length(g) + 1e-5);
    float2 f = tp.z * float2(N.y * wC, -N.x * wC);
    float4 s = scalar.read(uint2(c));
    f.y += BETA * s.r - KAPPA * s.b;
    float lx = fc.x - float(t) * TW;
    float nx = sin(lx * 0.55 + pass.time * 3.1 + float(t) * 1.7) * sin(fc.y * 0.31 - pass.time * 4.3)
             + 0.5 * sin(fc.y * 0.83 + pass.time * 7.9 + lx * 0.2);
    f.x += TURB * tp.w * nx * min(s.r, 1.5);
    float y = fc.y / TH, x = lx / TW;
    f.y += tp.y * (1.0 - smoothstep(0.05, 0.8, y)) * (0.7 + 0.3 * cos(6.2832 * (x - 0.5)));
    v += f * pass.dt;
    v *= 0.996;
    o.write(float4(v, 0.0, 1.0), uint2(c));
}

static float vnoise(float2 p) {
    float2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash3(float3(i, 7.0)), b = hash3(float3(i + float2(1, 0), 7.0));
    float c = hash3(float3(i + float2(0, 1), 7.0)), d = hash3(float3(i + float2(1, 1), 7.0));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

// combustion (Arrhenius), soot formation/oxidation, radiative loss, fuel + heat sources, bed heat
kernel void hearthReact(
    constant HearthSim& sim [[buffer(0)]],
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> scalar [[texture(0)]],
    texture2d<float, access::write> o [[texture(1)]],
    uint2 gid [[thread_position_in_grid]]) {
    const float A = 900.0, TA = 6000.0, KMIX = 2.0, THAD = 1.55, Y0 = 0.05, YIELD = 0.5;
    const float KOX = 5.0, KC = 2.6, KS = 1.5, KY = 1.0, KPYR = 0.6;
    int2 c = cellOf(gid, pass);
    int t = c.x / TWI;
    float2 fc = float2(c) + 0.5;
    float2 lp = float2(fc.x - float(t) * TW, fc.y);
    float dt = pass.dt;
    float4 s = scalar.read(uint2(c));
    float th = max(s.r, 0.0), Y = max(s.g, 0.0), S = max(s.b, 0.0);
    float T = 300.0 + 1000.0 * th;
    float Tig = max(T, mix(T, 300.0 + 1000.0 * sim.tiles[t].x, exp(-lp.y / 3.5)));
    float kin = A * exp(-TA / Tig);
    float hs = clamp((sim.sources[t * NS].y - 0.08) / 0.08, 0.0, 1.0), mixs = mix(4.0, 1.0, hs), kmix = KMIX * mixs;
    float k = 1.0 / (1.0 / max(kin, 1e-6) + 1.0 / kmix), f = 1.0 - exp(-k * dt);
    float burn = Y * f;
    Y -= burn;
    S += YIELD * burn;
    th += max(0.0, THAD - th) * f * Y / (Y + Y0) * 3.0;
    float pyr = min(Y, KPYR * Y * smoothstep(0.6, 1.1, th) * dt);
    Y -= pyr;
    S += pyr * 0.4 / (1.0 + S);
    S -= S * KOX * smoothstep(0.9, 1.6, th) * smoothstep(0.5, 0.0, Y) * dt;
    th *= exp(-KC * mixs * dt * (1.0 + 0.25 * th * th * th));
    S *= exp(-KS * mixs * dt);
    Y *= exp(-KY * dt);
    float n1 = vnoise(float2(fc.x / 3.0, pass.time * 6.0)), n2 = 0.5 + 0.5 * vnoise(float2(fc.x / 2.0 + 9.0, pass.time * 9.0));
    float band = exp(-pow((lp.y - 4.4) / 1.9, 2.0));
    for (int i = 0; i < NS; i++) {
        float4 q = sim.sources[t * NS + i];
        if (q.z <= 0.0) continue;
        float dx = (lp.x / TW - q.x) / q.y;
        float g = exp(-dx * dx) * band;
        Y += q.z * g * dt * (0.4 + 1.2 * n1);
        th += q.w * g * dt * (0.5 + n2);
    }
    float bed = 0.35 * sim.tiles[t].x;
    float by = exp(-lp.y / 0.8);
    th += (bed * (0.6 + 0.8 * n2) - th) * by * min(1.0, 4.0 * dt) * step(th, bed);
    o.write(float4(th, Y, S, 1.0), uint2(c));
}

kernel void hearthDivergence(
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> vel [[texture(0)]],
    texture2d<float, access::write> o [[texture(1)]],
    uint2 gid [[thread_position_in_grid]]) {
    int2 c = cellOf(gid, pass);
    int x0 = (c.x / TWI) * TWI;
    float2 C = vel.read(uint2(c)).xy;
    float2 L = c.x > x0 ? vel.read(uint2(c + int2(-1, 0))).xy : float2(-C.x, C.y);
    float2 R = c.x < x0 + TWI - 1 ? vel.read(uint2(c + int2(1, 0))).xy : float2(-C.x, C.y);
    float2 B = c.y > 0 ? vel.read(uint2(c + int2(0, -1))).xy : C;
    float2 T = c.y < THI - 1 ? vel.read(uint2(c + int2(0, 1))).xy : C;
    o.write(float4(0.5 * (R.x - L.x + T.y - B.y), 0.0, 0.0, 1.0), uint2(c));
}

// All Jacobi iterations for one tile in threadgroup memory: one dispatch, not
// twenty passes. Side walls are Neumann (mirror the centre); the floor and
// the open top are Dirichlet zero, as in the web pass.
kernel void hearthPressure(
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> pressure [[texture(0)]],
    texture2d<float> divergence [[texture(1)]],
    texture2d<float, access::write> o [[texture(2)]],
    uint group [[threadgroup_position_in_grid]],
    uint lid [[thread_index_in_threadgroup]]) {
    threadgroup float ping[CELLS];
    threadgroup float pong[CELLS];
    threadgroup float div[CELLS];
    uint x0 = (pass.lo + group) * uint(TWI);
    for (uint i = lid; i < uint(CELLS); i += PRESSURE_THREADS) {
        uint2 cell = uint2(x0 + i % uint(TWI), i / uint(TWI));
        ping[i] = pressure.read(cell).x;
        div[i] = divergence.read(cell).x;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (int k = 0; k < JACOBI; k++) {
        threadgroup float* from = (k & 1) == 0 ? ping : pong;
        threadgroup float* to = (k & 1) == 0 ? pong : ping;
        for (uint i = lid; i < uint(CELLS); i += PRESSURE_THREADS) {
            uint x = i % uint(TWI), y = i / uint(TWI);
            float C = from[i];
            float L = x > 0 ? from[i - 1] : C;
            float R = x < uint(TWI) - 1 ? from[i + 1] : C;
            float B = y > 0 ? from[i - uint(TWI)] : 0.0;
            float T = y < uint(THI) - 1 ? from[i + uint(TWI)] : 0.0;
            to[i] = 0.25 * (L + R + B + T - div[i]);
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }
    threadgroup float* result = (JACOBI & 1) == 0 ? ping : pong;
    for (uint i = lid; i < uint(CELLS); i += PRESSURE_THREADS) {
        o.write(float4(result[i], 0.0, 0.0, 1.0), uint2(x0 + i % uint(TWI), i / uint(TWI)));
    }
}

kernel void hearthProject(
    constant HearthPass& pass [[buffer(1)]],
    texture2d<float> pressure [[texture(0)]],
    texture2d<float> vel [[texture(1)]],
    texture2d<float, access::write> o [[texture(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    int2 c = cellOf(gid, pass);
    int x0 = (c.x / TWI) * TWI;
    float C = pressure.read(uint2(c)).x;
    float L = c.x > x0 ? pressure.read(uint2(c + int2(-1, 0))).x : C;
    float R = c.x < x0 + TWI - 1 ? pressure.read(uint2(c + int2(1, 0))).x : C;
    float B = c.y > 0 ? pressure.read(uint2(c + int2(0, -1))).x : 0.0;
    float T = c.y < THI - 1 ? pressure.read(uint2(c + int2(0, 1))).x : 0.0;
    float2 v = vel.read(uint2(c)).xy - 0.5 * float2(R - L, T - B);
    if (c.x == x0) v.x = max(v.x, 0.0);
    if (c.x == x0 + TWI - 1) v.x = min(v.x, 0.0);
    if (c.y == 0) v.y = max(v.y, 0.0);
    o.write(float4(v, 0.0, 1.0), uint2(c));
}

// ---------------- sparks ----------------

kernel void hearthSparkSpawn(
    constant HearthSpawn* spawns [[buffer(0)]],
    device HearthSpark* sparks [[buffer(1)]],
    uint id [[thread_position_in_grid]]) {
    sparks[spawns[id].slot.x] = spawns[id].spark;
}

// Kill a cleared tile's sparks, so a tile handed to another row starts clean.
kernel void hearthSparkKill(
    constant HearthSim& sim [[buffer(0)]],
    device HearthSpark* sparks [[buffer(1)]],
    uint id [[thread_position_in_grid]]) {
    float4 B = sparks[id].b;
    if (B.y > 0.0 && sim.flags[clamp(int(B.w + 0.5), 0, CAP - 1)].x > 0.0) sparks[id].b = float4(0.0);
}

kernel void hearthSparkUpdate(
    constant HearthPass& pass [[buffer(1)]],
    device HearthSpark* sparks [[buffer(2)]],
    texture2d<float> vel [[texture(0)]],
    uint id [[thread_position_in_grid]]) {
    const float DRAG = 0.9, GRAV = 12.0, COOL = 4.0e-10, BEDY = 4.6;
    float4 A = sparks[id].a, B = sparks[id].b;
    if (B.y <= 0.0) return;
    float dt = pass.dt;
    float x0 = B.w * TW;
    float2 p = A.xy, v = A.zw;
    float r = B.z;
    float2 u = vel.sample(cells, clampTile(p, x0)).xy;
    float cd = DRAG / (r * r);
    v = (v + dt * (cd * u + float2(0.0, -GRAV))) / (1.0 + dt * cd);
    p += v * dt;
    if (p.x < x0 + 0.4) { p.x = x0 + 0.4; v.x = abs(v.x) * 0.4; }
    if (p.x > x0 + TW - 0.4) { p.x = x0 + TW - 0.4; v.x = -abs(v.x) * 0.4; }
    float life = B.y - dt;
    if (p.y < BEDY) { p.y = BEDY; v.y = abs(v.y) * 0.2; v.x *= 0.5; life -= dt * 3.0; }
    float T = B.x;
    T = pow(1.0 / (T * T * T) + 3.0 * COOL / r * dt, -1.0 / 3.0);
    if (T < 680.0 || p.y > TH + 2.0) life = 0.0;
    sparks[id].a = float4(p, v);
    sparks[id].b = float4(T, life, r, B.w);
}

// Flame height probe: per tile, the height below which 96% of its luminous
// emission lies, plus the total.
kernel void hearthMeasure(
    constant HearthPass& pass [[buffer(1)]],
    device float4* out [[buffer(2)]],
    texture2d<float> scalar [[texture(0)]],
    texture2d<float> lut [[texture(1)]],
    uint gid [[thread_position_in_grid]]) {
    int t = int(pass.lo + gid);
    int x0 = t * TWI;
    float rows[THI];
    float tot = 0.0;
    for (int y = 0; y < THI; y++) {
        float r = 0.0;
        if (y >= 5) {
            for (int x = 1; x < TWI - 1; x++) {
                float4 s = scalar.read(uint2(x0 + x, y));
                r += min(emitL(s, bb(lut, 300.0 + 1000.0 * max(s.r, 0.0))), 1.0);
            }
        }
        rows[y] = r;
        tot += r;
    }
    float h = 0.0, acc = 0.0;
    if (tot > 1.5) {
        for (int y = 0; y < THI; y++) {
            acc += rows[y];
            if (acc >= 0.96 * tot) { h = (float(y) + 1.0) / TH; break; }
        }
    }
    out[t] = float4(h, tot, 0.0, 1.0);
}

// ---------------- compositing ----------------

// Rects are in the layer's pixels, top-left origin: (x, y, width, height).
// clip is (minX, minY, maxX, maxY).
struct HearthDraw {
    float4 rect;
    float4 glowRect;
    float4 clip;
    float4 bed;   // surface K, core K, seed, ash
    float4 glow;  // rgb intensity, on
    float2 viewport;
    float time;
    float dpr;
    int tile;
    int light;
};

struct QuadOut {
    float4 position [[position]];
    float2 uv;
};

static float4 placeQuad(float4 r, float2 k, float2 viewport) {
    float2 p = float2(r.x + k.x * r.z, r.y + r.w - k.y * r.w);
    return float4(p.x / viewport.x * 2.0 - 1.0, 1.0 - p.y / viewport.y * 2.0, 0.0, 1.0);
}

static bool clipped(float2 frag, float4 clip) {
    return frag.x < clip.x || frag.y < clip.y || frag.x > clip.z || frag.y > clip.w;
}

vertex QuadOut hearthTileVertex(constant HearthDraw& d [[buffer(0)]], uint vid [[vertex_id]]) {
    float2 k = float2(float(vid & 1), float((vid >> 1) & 1));
    QuadOut o;
    o.position = placeQuad(d.rect, k, d.viewport);
    o.uv = k; // y = 0 at the bed
    return o;
}

vertex QuadOut hearthGlowVertex(constant HearthDraw& d [[buffer(0)]], uint vid [[vertex_id]]) {
    float2 k = float2(float(vid & 1), float((vid >> 1) & 1));
    QuadOut o;
    o.position = d.glow.a > 0.0 ? placeQuad(d.glowRect, k, d.viewport) : float4(2.0, 2.0, 2.0, 1.0);
    o.uv = k;
    return o;
}

// Light spill: a soft warm pool on the row around a lit fire, additive,
// centred low on the flame.
fragment float4 hearthGlowFragment(QuadOut in [[stage_in]], constant HearthDraw& d [[buffer(0)]]) {
    if (clipped(in.position.xy, d.clip)) discard_fragment();
    float2 q = (in.uv - float2(0.5, 0.3)) * float2(2.0, 1.7);
    float g = exp(-dot(q, q) * 2.6) * (1.0 - smoothstep(0.75, 1.0, length(q))) * smoothstep(0.0, 0.3, in.uv.y);
    return float4(d.glow.rgb * g, 0.0);
}

static float4 cubic(texture2d<float> s, float2 p) {
    p -= 0.5;
    float2 i = floor(p), f = p - i;
    float2 f2 = f * f, f3 = f2 * f;
    float2 w0 = (1.0 - 3.0 * f + 3.0 * f2 - f3) / 6.0, w1 = (4.0 - 6.0 * f2 + 3.0 * f3) / 6.0;
    float2 w2 = (1.0 + 3.0 * f + 3.0 * f2 - 3.0 * f3) / 6.0, w3 = f3 / 6.0;
    float2 g0 = w0 + w1, g1 = w2 + w3;
    float2 h0 = i - 0.5 + w1 / g0, h1 = i + 1.5 + w3 / g1;
    return g0.y * (g0.x * s.sample(cells, float2(h0.x, h0.y)) + g1.x * s.sample(cells, float2(h1.x, h0.y)))
         + g1.y * (g0.x * s.sample(cells, float2(h0.x, h1.y)) + g1.x * s.sample(cells, float2(h1.x, h1.y)));
}

static float2 h2(float2 p) {
    p = float2(dot(p, float2(127.1, 311.7)), dot(p, float2(269.5, 183.3)));
    return fract(sin(p) * 43758.5453);
}

static float3 voro(float2 x, thread float2& rel) {
    float2 n = floor(x), f = fract(x);
    float d1 = 8.0, d2 = 8.0, id = 0.0;
    rel = float2(0.0);
    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            float2 g = float2(i, j);
            float2 r = g + h2(n + g) * 0.8 + 0.1 - f;
            float d = dot(r, r);
            if (d < d1) { d2 = d1; d1 = d; id = h2(n + g + 17.0).x; rel = -r; }
            else if (d < d2) d2 = d;
        }
    }
    return float3(sqrt(d1), sqrt(d2), id);
}

static float vn(float x) {
    float i = floor(x), f = fract(x);
    float a = fract(sin(i * 91.3) * 437.5), b = fract(sin((i + 1.0) * 91.3) * 437.5);
    return mix(a, b, f * f * (3.0 - 2.0 * f));
}

fragment float4 hearthTileFragment(
    QuadOut in [[stage_in]],
    constant HearthDraw& d [[buffer(0)]],
    texture2d<float> u_s [[texture(0)]],
    texture2d<float> u_vel [[texture(1)]],
    texture2d<float> lut [[texture(2)]]) {
    const float K_A = 0.9, EXPO_BED = 3.4, BEDH = 7.0;
    if (clipped(in.position.xy, d.clip)) discard_fragment();
    float x0 = float(d.tile) * TW;
    float2 lp = in.uv * float2(TW, TH);
    float4 s = cubic(u_s, float2(clamp(lp.x, 1.5, TW - 1.5) + x0, clamp(lp.y, 1.5, TH - 1.0)));
    float th = max(s.r, 0.0), soot = max(s.b, 0.0);
    float4 B = bb(lut, 300.0 + 1000.0 * th);
    // No box: the fire is light on the page. Emission and smoke fade out
    // softly well inside every edge, so nothing ends on a straight line.
    float xn = in.uv.x;
    float fade = (1.0 - smoothstep(TH * 0.55, TH * 0.93, lp.y)) * smoothstep(0.02, 0.24, xn) * smoothstep(0.98, 0.76, xn);
    float3 col = B.rgb * emitL(s, B) * fade;
    float a = (1.0 - exp(-K_A * soot)) * (1.0 - smoothstep(0.2, 0.8, th)) * 0.35 * fade;
    col += float3(0.020, 0.014, 0.010) * a;
    float4 bd = d.bed;
    // The bed is a low pile on the row's baseline: a mound in the middle of
    // the tile, smaller once it has burnt to ash.
    float mound = clamp(1.0 - pow((xn - 0.5) / mix(0.3, 0.2, bd.w), 2.0), 0.0, 1.0);
    float top = BEDH * mix(1.0, 0.55, bd.w) * (0.55 + 0.45 * vn(lp.x * 0.3 + bd.z * 13.0)) * sqrt(mound);
    float2 bq = float2(lp.x * 0.2, lp.y * 0.3 + lp.x * 0.035) + bd.z * 7.13;
    float2 rel;
    float3 vr = voro(bq, rel);
    float edge = vr.y - vr.x;
    float lump = smoothstep(0.07, 0.2, edge) * (1.0 - smoothstep(0.5, 0.68, vr.x));
    float bm = max(1.0 - smoothstep(top - 0.6, top, lp.y), lump * (1.0 - smoothstep(top + 0.2, top + 1.0, lp.y))) * smoothstep(0.0, 0.3, mound);
    if (bm > 0.0) {
        float thb = min(u_s.sample(cells, float2(x0 + clamp(lp.x, 0.5, TW - 0.5), 4.0)).r, 1.3);
        float vb = length(u_vel.sample(cells, float2(x0 + clamp(lp.x, 0.5, TW - 0.5), 2.0)).xy);
        float depth = clamp(1.0 - lp.y / max(top, 0.5), 0.0, 1.0);
        float lit = smoothstep(600.0, 950.0, bd.x);
        float dome = sqrt(max(0.0, 1.0 - vr.x * vr.x * 2.2));
        float rim = 1.0 - smoothstep(0.05, 0.32, edge);
        float Tg = max(bd.x, bd.y) + 60.0 + 180.0 * thb + 50.0 * clamp(vb / 12.0, 0.0, 1.0) * lit;
        float Tf = bd.x + 200.0 * thb + ((vr.z - 0.5) * 150.0 - 160.0 * dome - 120.0 * clamp(rel.y * 2.2, -1.0, 1.0)
                 + 60.0 * clamp(vb / 12.0, 0.0, 1.0)) * lit - 60.0 * (1.0 - depth);
        float Ts = mix(Tf, Tg, rim * 0.8);
        float Tb = mix(Tg, Ts, lump);
        float4 BB = bb(lut, Tb);
        float3 eb = BB.rgb * pow(BB.a, 3.5) * EXPO_BED;
        float3 flameLight = bb(lut, 300.0 + 1000.0 * thb).rgb * clamp(thb * 1.2, 0.0, 1.0);
        float up = clamp(0.3 + 0.5 * dome + 0.9 * rel.y, 0.0, 1.2);
        float3 alb = lump * (float3(0.009, 0.008, 0.0075) * (0.4 + 0.9 * up) + 0.14 * flameLight * up);
        // ash: a burnt-out bed's lumps go pale grey; the gaps stay dark
        float3 ash = float3(0.085, 0.080, 0.074) * (0.45 + 0.8 * up) * (0.75 + 0.5 * vr.z);
        alb = mix(alb, lump * ash + (1.0 - lump) * float3(0.012, 0.011, 0.010), bd.w);
        col = mix(col, eb + alb, bm);
        a = mix(a, mix(1.0, 0.75, bd.w), bm);
    }
    col = 1.0 - exp(-col);
    col = pow(col, float3(1.0 / 2.2));
    // Light, not paint: colour is added over the row, alpha only for soot
    // and coals (blend one, one-minus-source-alpha), as on the web. Added
    // light vanishes on a light page, so there the flame also covers what
    // is under it in proportion to its brightness.
    if (d.light != 0) a = max(a, min(1.0, 1.4 * max(col.r, max(col.g, col.b))));
    return float4(col, a);
}

struct SparkOut {
    float4 position [[position]];
    float point_size [[point_size]];
    float3 color;
    float alpha;
};

struct SparkLineOut {
    float4 position [[position]];
    float3 color;
    float alpha;
};

static bool sparkVertex(constant HearthDraw& d, const device HearthSpark* sparks, texture2d<float> lut,
                        int id, int tail, thread float4& position, thread float& size, thread float3& color) {
    float4 A = sparks[id].a, B = sparks[id].b;
    if (B.y <= 0.0 || int(B.w + 0.5) != d.tile) return false;
    float2 p = A.xy - float(tail) * A.zw * 0.06;
    float2 uv = float2((p.x - float(d.tile) * TW) / TW, p.y / TH);
    position = placeQuad(d.rect, uv, d.viewport);
    float sc = d.rect.z / TW;
    size = clamp((0.35 + B.z * 0.45) * sc, 1.5 * d.dpr, 4.0 * d.dpr);
    float4 k = bb(lut, B.x);
    float fade = smoothstep(0.0, 0.35, B.y);
    color = k.rgb * pow(k.a, 2.2) * 3.2 * fade;
    return true;
}

vertex SparkOut hearthSparkPointVertex(
    constant HearthDraw& d [[buffer(0)]],
    const device HearthSpark* sparks [[buffer(1)]],
    texture2d<float> lut [[texture(0)]],
    uint vid [[vertex_id]]) {
    SparkOut o;
    o.alpha = 1.0;
    if (!sparkVertex(d, sparks, lut, int(vid), 0, o.position, o.point_size, o.color)) {
        o.position = float4(2.0, 2.0, 2.0, 1.0);
        o.point_size = 0.0;
        o.color = float3(0.0);
    }
    return o;
}

vertex SparkLineOut hearthSparkLineVertex(
    constant HearthDraw& d [[buffer(0)]],
    const device HearthSpark* sparks [[buffer(1)]],
    texture2d<float> lut [[texture(0)]],
    uint vid [[vertex_id]]) {
    SparkLineOut o;
    int tail = int(vid & 1);
    float size;
    o.alpha = tail == 1 ? 0.0 : 1.0;
    if (!sparkVertex(d, sparks, lut, int(vid >> 1), tail, o.position, size, o.color)) {
        o.position = float4(2.0, 2.0, 2.0, 1.0);
        o.color = float3(0.0);
    }
    return o;
}

static float4 sparkColor(float3 c, float f, int light) {
    float3 l = pow(1.0 - exp(-c * f * 1.6), float3(1.0 / 2.2));
    // Added light on dark; on a light page the spark also covers the page.
    return float4(l, light != 0 ? min(1.0, 1.4 * max(l.r, max(l.g, l.b))) : 0.0);
}

fragment float4 hearthSparkPointFragment(SparkOut in [[stage_in]], constant HearthDraw& d [[buffer(0)]],
                                         float2 pc [[point_coord]]) {
    if (clipped(in.position.xy, d.clip)) discard_fragment();
    float r = length(pc - 0.5) * 2.0;
    float f = exp(-r * r * 2.8) * (1.0 - smoothstep(0.75, 1.0, r));
    return sparkColor(in.color, f, d.light);
}

fragment float4 hearthSparkLineFragment(SparkLineOut in [[stage_in]], constant HearthDraw& d [[buffer(0)]]) {
    if (clipped(in.position.xy, d.clip)) discard_fragment();
    return sparkColor(in.color, 0.55 * in.alpha, d.light);
}
"""#
}
