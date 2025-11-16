export type RadialVisualizerOptions = {
  /** Callback invoked with normalized mic level when internal analyser is running */
  onLevel?: (level: number) => void;
  /** Extra canvas inset (px) applied beyond the container bounds */
  outerInset?: number;
};

/**
 * Canvas-based radial audio visualizer that supports both internal microphone
 * capture (for push-to-talk) and external level updates (for remote audio).
 */
export class RadialVisualizer {
  private readonly container: HTMLElement;
  private readonly canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D | null = null;
  private audioCtx: AudioContext | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private analyser: AnalyserNode | null = null;
  private freqData: Uint8Array<ArrayBuffer> | null = null;
  private timeDomain: Uint8Array<ArrayBuffer> | null = null;
  private stream: MediaStream | null = null;
  private ownsStream = false;
  private running = false;
  private readonly onLevel?: (level: number) => void;
  private animationId: number | null = null;
  private lastTimestamp = 0;
  private targetLevel = 0;
  private displayLevel = 0;
  private renderColor = '#3b82f6';
  private renderState = 0;
  private pulseClock = 0;
  private readonly outerInset: number;

  constructor(container: HTMLElement, options: RadialVisualizerOptions = {}) {
    this.container = container;
    this.onLevel = options.onLevel;
    this.outerInset = options.outerInset ?? 20;

    this.canvas = document.createElement('canvas');
    this.canvas.id = 'radialViz';
    this.canvas.style.position = 'absolute';
    this.canvas.style.pointerEvents = 'none';
    this.canvas.style.filter = 'drop-shadow(0 0 32px rgba(139,92,246,0.45))';
    this.canvas.style.zIndex = '5';
    this.container.style.position = 'relative';
    this.container.prepend(this.canvas);
    this.ctx = this.canvas.getContext('2d');

    this.resize();
    window.addEventListener('resize', this.resize, { passive: true });
    this.animationId = requestAnimationFrame(this.animate);
  }

  provideStream(stream: MediaStream | null): void {
    this.stream = stream;
    this.ownsStream = false;
  }

  async start(): Promise<void> {
    if (this.running) return;

    if (!this.audioCtx) {
      this.audioCtx = new (window.AudioContext || (window as any).webkitAudioContext)();
    }

    if (this.audioCtx.state === 'suspended') {
      await this.audioCtx.resume().catch(() => undefined);
    }

    try {
      if (!this.stream) {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            channelCount: 1,
          },
        });
        this.ownsStream = true;
      }

      this.source = this.audioCtx.createMediaStreamSource(this.stream);
      this.analyser = this.audioCtx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.82;
      this.source.connect(this.analyser);
      this.freqData = new Uint8Array(new ArrayBuffer(this.analyser.frequencyBinCount));
      this.timeDomain = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));
      this.running = true;
    } catch (error) {
      this.cleanupAudioGraph();
      if (this.stream && this.ownsStream) {
        this.stream.getTracks().forEach(track => track.stop());
        this.stream = null;
        this.ownsStream = false;
      }
      throw error;
    }
  }

  stop(): void {
    this.running = false;
    this.cleanupAudioGraph();
    this.freqData = null;
    this.timeDomain = null;
    if (this.stream && this.ownsStream) {
      this.stream.getTracks().forEach(track => track.stop());
      this.stream = null;
      this.ownsStream = false;
    }
    this.targetLevel = 0;
    if (this.onLevel) this.onLevel(0);
  }

  destroy(): void {
    this.stop();
    window.removeEventListener('resize', this.resize);
    if (this.animationId != null) cancelAnimationFrame(this.animationId);
    this.animationId = null;
    if (this.audioCtx) {
      this.audioCtx.close().catch(() => undefined);
      this.audioCtx = null;
    }
    this.canvas.remove();
  }

  render(level: number, color: string, state: number): void {
    this.targetLevel = Math.min(1, Math.max(0, level));
    this.renderColor = color;
    this.renderState = state;
    // If analyser is paused, ensure canvas still updates
    if (!this.running) {
      this.drawFrame(0);
    }
  }

  private cleanupAudioGraph(): void {
    if (this.source) {
      this.source.disconnect();
      this.source = null;
    }
    if (this.analyser) {
      this.analyser.disconnect();
      this.analyser = null;
    }
  }

  private resize = (): void => {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.container.getBoundingClientRect();
    const rectWidth = rect.width || this.container.offsetWidth || this.container.clientWidth || 0;
    const rectHeight = rect.height || this.container.offsetHeight || this.container.clientHeight || 0;
    const base = Math.max(rectWidth, rectHeight);
    const sizeCss = Math.max(160, base + this.outerInset * 2);
    this.canvas.style.width = `${sizeCss}px`;
    this.canvas.style.height = `${sizeCss}px`;
    this.canvas.style.left = `50%`;
    this.canvas.style.top = `50%`;
    this.canvas.style.transform = `translate(-50%, -50%)`;
    this.canvas.width = Math.floor(sizeCss * dpr);
    this.canvas.height = Math.floor(sizeCss * dpr);
    if (this.ctx) {
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
  };

  private animate = (timestamp: number): void => {
    const dt = this.lastTimestamp ? (timestamp - this.lastTimestamp) / 1000 : 0;
    this.lastTimestamp = timestamp;

    if (this.running && this.analyser) {
      if (!this.freqData || this.freqData.length !== this.analyser.frequencyBinCount) {
        this.freqData = new Uint8Array(new ArrayBuffer(this.analyser.frequencyBinCount));
      }
      if (!this.timeDomain || this.timeDomain.length !== this.analyser.fftSize) {
        this.timeDomain = new Uint8Array(new ArrayBuffer(this.analyser.fftSize));
      }

      const freqBuffer = this.freqData as Uint8Array<ArrayBuffer>;
      this.analyser.getByteFrequencyData(freqBuffer);
      if (this.onLevel && this.timeDomain) {
        const timeBuffer = this.timeDomain as Uint8Array<ArrayBuffer>;
        this.analyser.getByteTimeDomainData(timeBuffer);
        const level = this.calculateLevel(timeBuffer);
        this.onLevel(level);
      }
    }

    this.drawFrame(dt);
    this.animationId = requestAnimationFrame(this.animate);
  };

  private drawFrame(dt: number): void {
    if (!this.ctx) return;

    const ctx = this.ctx;
    const rect = this.canvas.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;

    // Smoothly approach target level
    const lerp = this.running ? 0.25 : 0.18;
    this.displayLevel += (this.targetLevel - this.displayLevel) * lerp;
    if (Math.abs(this.targetLevel) < 0.001 && Math.abs(this.displayLevel) < 0.002) {
      this.displayLevel = 0;
    }

    this.pulseClock += dt;

    ctx.clearRect(0, 0, w, h);

    const cx = w / 2;
    const cy = h / 2;
    const minDim = Math.min(w, h);
    const baseR = Math.max(45, (minDim / 2) - 15);
    const innerR = baseR * 0.6;

    const micActive = (this.renderState & 1) === 1;
    const assistantActive = (this.renderState & 2) === 2;

    const intensity = this.displayLevel;
    const accent = this.renderColor;

    // Ambient glow - hide when idle, show when active
    if (micActive || assistantActive) {
      const ambientGradient = ctx.createRadialGradient(cx, cy, innerR * 0.4, cx, cy, baseR + 36);
      const glowOpacity = 0.2 + intensity * 0.35;
      ambientGradient.addColorStop(0, `rgba(99,102,241,${glowOpacity})`);
      ambientGradient.addColorStop(1, 'rgba(15,23,42,0)');
      ctx.beginPath();
      ctx.arc(cx, cy, baseR + 32, 0, Math.PI * 2);
      ctx.fillStyle = ambientGradient;
      ctx.fill();
    }


    // Render spectrum bars using analyser data when available; otherwise synthesize
    const bars = 120;
    let data: number[] = [];

    if (this.freqData && this.freqData.length > 0) {
      const step = Math.floor(this.freqData.length / bars);
      for (let i = 0; i < bars; i++) {
        const idx = i * step;
        const v = this.freqData[Math.min(idx, this.freqData.length - 1)] / 255;
        data.push(Math.pow(v, 1.4));
      }
    } else {
      const phase = this.pulseClock * (assistantActive ? 1.5 : 1.1);
      for (let i = 0; i < bars; i++) {
        const wave = Math.sin(phase + (i / bars) * Math.PI * 2);
        const amp = 0.6 + 0.4 * Math.sin(phase * 0.6 + i * 0.05);
        data.push(Math.max(0, intensity * amp * Math.abs(wave)));
      }
    }

    const innerAlpha = micActive || assistantActive ? 0.28 + intensity * 0.4 : 0.18;
    const barColor = ctx.createLinearGradient(cx, cy - innerR, cx, cy + baseR);
    barColor.addColorStop(0, assistantActive && !micActive ? '#38bdf8' : accent);
    barColor.addColorStop(1, micActive && !assistantActive ? '#f472b6' : '#6366f1');

    ctx.lineWidth = 3;
    ctx.strokeStyle = barColor;

    for (let i = 0; i < bars; i++) {
      const value = data[i];
      const eased = Math.pow(Math.min(1, value), 1.2);
      const radius = innerR + eased * (baseR - innerR);
      const angle = (i / bars) * Math.PI * 2;
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      const x1 = cx + cos * innerR;
      const y1 = cy + sin * innerR;
      const x2 = cx + cos * (radius + 6 + intensity * 8);
      const y2 = cy + sin * (radius + 6 + intensity * 8);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }

    // Inner core
    const coreGradient = ctx.createRadialGradient(cx, cy, innerR * 0.2, cx, cy, innerR);
    coreGradient.addColorStop(0, `rgba(15,23,42,${0.5 + intensity * 0.2})`);
    coreGradient.addColorStop(1, `rgba(15,23,42,${0.05})`);
    ctx.beginPath();
    ctx.arc(cx, cy, innerR, 0, Math.PI * 2);
    ctx.fillStyle = coreGradient;
    ctx.fill();

    // Highlight pulses for mic / assistant activity
    if (micActive || assistantActive) {
      const pulseCount = micActive && assistantActive ? 3 : 2;
      for (let i = 0; i < pulseCount; i++) {
        const progress = (this.pulseClock * (micActive ? 1.45 : 1.1) + i * 0.6) % 1;
        const pulseRadius = innerR + progress * (baseR - innerR);
        const alpha = Math.max(0, 0.35 - progress * (0.5 + intensity * 0.5));
        ctx.beginPath();
        ctx.arc(cx, cy, pulseRadius, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(236,72,153,${micActive ? alpha : alpha * 0.6})`;
        ctx.lineWidth = 1.5 + intensity * 1.5;
        ctx.stroke();
      }
    }
  }

  private calculateLevel(data: Uint8Array): number {
    if (!data.length) return 0;
    let sumSquares = 0;
    for (let i = 0; i < data.length; i++) {
      const centered = (data[i] - 128) / 128;
      sumSquares += centered * centered;
    }
    const rms = Math.sqrt(sumSquares / data.length);
    return Math.min(1, rms * 2);
  }
}
