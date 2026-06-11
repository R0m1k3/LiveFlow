// Rééchantillonne le micro (fréquence native du navigateur, ex. 48 kHz)
// vers du PCM Int16 mono 16 kHz, attendu par le serveur.
class PCMDownsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.buffer = new Float32Array(0);
    this.readPos = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;

    const buf = new Float32Array(this.buffer.length + input.length);
    buf.set(this.buffer);
    buf.set(input, this.buffer.length);

    const outLen = Math.floor((buf.length - 1 - this.readPos) / this.ratio);
    if (outLen > 0) {
      const out = new Int16Array(outLen);
      let pos = this.readPos;
      for (let i = 0; i < outLen; i++) {
        const i0 = Math.floor(pos);
        const frac = pos - i0;
        const s = buf[i0] * (1 - frac) + buf[i0 + 1] * frac;
        out[i] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
        pos += this.ratio;
      }
      this.readPos = pos;
      this.port.postMessage(out.buffer, [out.buffer]);
    }

    const keep = Math.floor(this.readPos);
    this.buffer = buf.slice(keep);
    this.readPos -= keep;
    return true;
  }
}

registerProcessor('pcm-downsampler', PCMDownsampler);
