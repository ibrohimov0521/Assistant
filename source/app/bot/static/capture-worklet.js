class SaidaCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(4096);
    this.used = 0;
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i += 1) {
      this.buffer[this.used++] = input[i];
      if (this.used === this.buffer.length) {
        this.port.postMessage(this.buffer.buffer, [this.buffer.buffer]);
        this.buffer = new Float32Array(4096);
        this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor("saida-capture", SaidaCapture);
