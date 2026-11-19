import { ref } from 'vue'

// Local speech-to-text: the clip never leaves the browser. Whisper runs in WASM
// via transformers.js; the model ships with the app under /static/asr (~79MB, so
// no first-run download and no key).
const MODEL = 'whisper-base'
const RATE = 16000

let asrPromise = null

function loadAsr(onProgress) {
  if (!asrPromise) {
    asrPromise = (async () => {
      const { env, pipeline } = await import('@huggingface/transformers')
      env.allowRemoteModels = false
      env.allowLocalModels = true
      env.localModelPath = `${import.meta.env.BASE_URL}static/asr/`
      return pipeline('automatic-speech-recognition', MODEL, { dtype: 'q8', progress_callback: onProgress })
    })().catch((e) => {
      asrPromise = null
      throw e
    })
  }
  return asrPromise
}

// Whisper expects 16k mono; the recorder usually already is.
function to16k(samples, rate) {
  if (rate === RATE || !samples.length) return samples
  const out = new Float32Array(Math.round((samples.length * RATE) / rate))
  const step = rate / RATE
  for (let i = 0; i < out.length; i += 1) {
    const at = i * step
    const i0 = Math.floor(at)
    const i1 = Math.min(i0 + 1, samples.length - 1)
    out[i] = samples[i0] + (samples[i1] - samples[i0]) * (at - i0)
  }
  return out
}

export function useVoice({ onText, onLevel, maxSeconds = 60 } = {}) {
  const recording = ref(false)
  const transcribing = ref(false)
  const status = ref('') // 加载语音模型… / 转写中…
  const supported =
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === 'function' &&
    typeof AudioContext === 'function'

  let ctx = null
  let stream = null
  let source = null
  let processor = null
  let sink = null
  let chunks = []
  let timer = null

  function teardown() {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
    try {
      source && source.disconnect()
      processor && processor.disconnect()
      sink && sink.disconnect()
    } catch (e) {
      /* nodes may already be detached */
    }
    if (stream) stream.getTracks().forEach((t) => t.stop())
    if (ctx && ctx.state !== 'closed') ctx.close().catch(() => {})
    ctx = stream = source = processor = sink = null
  }

  async function start() {
    if (transcribing.value || recording.value) return
    if (!supported) return uni.showToast({ title: '当前浏览器不支持录音', icon: 'none' })
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      })
    } catch (e) {
      return uni.showToast({ title: '无法使用麦克风，请检查权限', icon: 'none' })
    }
    ctx = new AudioContext()
    // 16k mono is what whisper wants and keeps the clip small; browsers that
    // refuse the rate fall back to their own and to16k() resamples.
    try {
      await ctx.close()
      ctx = new AudioContext({ sampleRate: RATE })
    } catch (e) {
      ctx = new AudioContext()
    }
    source = ctx.createMediaStreamSource(stream)
    processor = ctx.createScriptProcessor(4096, 1, 1)
    chunks = []
    processor.onaudioprocess = (e) => {
      if (!recording.value) return
      const data = e.inputBuffer.getChannelData(0)
      chunks.push(new Float32Array(data))
      if (onLevel) {
        let sum = 0
        for (let i = 0; i < data.length; i += 4) sum += data[i] * data[i]
        onLevel(Math.min(1, Math.sqrt(sum / (data.length / 4)) * 8))
      }
    }
    // Keep the graph pulling samples without echoing the mic to the speakers.
    sink = ctx.createGain()
    sink.gain.value = 0
    source.connect(processor)
    processor.connect(sink)
    sink.connect(ctx.destination)
    recording.value = true
    timer = setTimeout(() => stop(), maxSeconds * 1000)
  }

  async function stop() {
    if (!recording.value) return
    recording.value = false
    const rate = ctx ? ctx.sampleRate : RATE
    teardown()

    const total = chunks.reduce((n, c) => n + c.length, 0)
    const samples = new Float32Array(total)
    let at = 0
    for (const c of chunks) {
      samples.set(c, at)
      at += c.length
    }
    chunks = []
    if (total < rate * 0.3) return uni.showToast({ title: '说话时间太短', icon: 'none' })

    transcribing.value = true
    status.value = '加载语音模型…'
    try {
      const asr = await loadAsr((p) => {
        if (p && p.status === 'progress' && p.total) status.value = `加载语音模型 ${Math.round((p.loaded / p.total) * 100)}%`
      })
      status.value = '转写中…'
      const out = await asr(to16k(samples, rate), { language: 'chinese', task: 'transcribe' })
      const text = (out && out.text ? out.text : '').trim()
      if (!text) uni.showToast({ title: '没听清，请再说一遍', icon: 'none' })
      else onText && onText(text)
    } catch (e) {
      uni.showToast({ title: `语音转写失败：${(e && e.message) || e}`, icon: 'none' })
    } finally {
      transcribing.value = false
      status.value = ''
    }
  }

  // Leaving the page mid-recording: drop the clip instead of transcribing it.
  function cancel() {
    if (!recording.value) return
    recording.value = false
    chunks = []
    teardown()
  }

  return { recording, transcribing, status, supported, start, stop, cancel }
}
