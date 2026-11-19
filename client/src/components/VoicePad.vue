<script setup>
import { nextTick, onUnmounted, ref } from 'vue'

import { useVoice } from '../voice'

const props = defineProps({ disabled: { type: Boolean, default: false } })
const emit = defineEmits(['transcript'])

const levels = ref([8, 8, 8, 8, 8])
const seconds = ref(0)
const converting = ref(false) // finger is over the 转文字 hotspot

let tick = null
let convertBox = null
let docMove = null
let docUp = null
let pressing = false // pointerdown + mousedown + touchstart all fire for one press

const { recording, transcribing, status, supported, start, stop, cancel } = useVoice({
  onText: (text) => emit('transcript', text),
  onLevel: (level) => {
    const weight = [0.7, 1, 0.55, 0.9, 0.65]
    levels.value = weight.map((w) => 6 + Math.round(level * w * 30))
  },
})

function point(e) {
  const t = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e
  return { x: t.clientX, y: t.clientY }
}

// The hotspot only exists while the panel is up, so it is measured on press.
async function measureConvert() {
  await nextTick()
  const el = typeof document !== 'undefined' ? document.getElementById('vp-convert') : null
  if (!el) {
    convertBox = null
    return
  }
  const r = el.getBoundingClientRect()
  convertBox = { left: r.left, right: r.right, top: r.top, bottom: r.bottom }
}

// The finger leaves the button as soon as it slides, so drag/up are tracked on
// the document; pointer events on the button alone never see them.
function attachDrag() {
  if (typeof document === 'undefined') return
  const pointer = typeof window !== 'undefined' && 'PointerEvent' in window
  docMove = (e) => onMove(e)
  docUp = () => onUp()
  const moves = pointer ? ['pointermove'] : ['touchmove', 'mousemove']
  const ups = pointer ? ['pointerup', 'pointercancel'] : ['touchend', 'touchcancel', 'mouseup']
  moves.forEach((n) => document.addEventListener(n, docMove, { passive: true }))
  ups.forEach((n) => document.addEventListener(n, docUp))
}

function detachDrag() {
  if (!docMove || typeof document === 'undefined') return
  ;['pointermove', 'touchmove', 'mousemove'].forEach((n) => document.removeEventListener(n, docMove))
  ;['pointerup', 'pointercancel', 'touchend', 'touchcancel', 'mouseup'].forEach((n) => document.removeEventListener(n, docUp))
  docMove = null
  docUp = null
}

function stopTick() {
  if (tick) {
    clearInterval(tick)
    tick = null
  }
}

async function onDown(e) {
  if (pressing || props.disabled || recording.value || transcribing.value) return
  if (!supported) return uni.showToast({ title: '当前浏览器不支持录音', icon: 'none' })
  pressing = true
  converting.value = false
  await start()
  pressing = false
  if (!recording.value) return
  attachDrag()
  seconds.value = 0
  tick = setInterval(() => {
    seconds.value += 1
  }, 1000)
  await measureConvert()
}

function onMove(e) {
  if (!recording.value) return
  const p = point(e)
  const b = convertBox
  converting.value = !!(b && p.x >= b.left - 14 && p.x <= b.right + 14 && p.y >= b.top - 14 && p.y <= b.bottom + 14)
}

async function onUp() {
  if (!recording.value) return
  stopTick()
  detachDrag()
  converting.value = false
  await stop()
}

onUnmounted(() => {
  stopTick()
  detachDrag()
  cancel()
})
</script>

<template>
  <view class="voice">
    <view
      class="hold"
      :class="{ on: recording, off: disabled }"
      @pointerdown.stop.prevent="onDown"
      @mousedown.stop.prevent="onDown"
      @touchstart.stop.prevent="onDown"
    >
      <text v-if="transcribing">{{ status || '转写中…' }}</text>
      <text v-else-if="recording">松开 转文字</text>
      <text v-else>按住 说话</text>
    </view>

    <view v-if="recording" class="mask">
      <view class="panel">
        <view id="vp-convert" class="convert" :class="{ hot: converting }">
          {{ converting ? '松开 转文字' : '转文字' }}
        </view>
        <view class="meter">
          <view v-for="(h, i) in levels" :key="i" class="bar" :style="{ height: h + 'px' }"></view>
        </view>
        <view class="hint">{{ converting ? '松开，转成文字发送' : '滑到右上角转文字 · 松手发送' }}</view>
        <view class="secs">{{ seconds }}s</view>
      </view>
    </view>
  </view>
</template>

<style scoped>
.voice {
  flex: 1;
  min-width: 0;
}
.hold {
  height: 40px;
  line-height: 40px;
  text-align: center;
  font-size: 14px;
  color: #333;
  background: #f2f3f5;
  border-radius: 8px;
  user-select: none;
  touch-action: none;
}
.hold.on {
  background: #e2e4e8;
  color: #666;
}
.hold.off {
  opacity: 0.5;
}
.mask {
  position: fixed;
  left: 0;
  right: 0;
  top: 0;
  bottom: 0;
  z-index: 999;
  display: flex;
  justify-content: center;
  padding-top: 26vh;
  background: rgba(0, 0, 0, 0.28);
  pointer-events: none;
}
.panel {
  position: relative;
  width: 210px;
  padding: 22px 18px 14px;
  border-radius: 14px;
  background: rgba(28, 28, 30, 0.94);
  text-align: center;
}
.convert {
  position: absolute;
  top: -15px;
  right: -10px;
  padding: 5px 12px;
  border-radius: 15px;
  border: 1px solid rgba(255, 255, 255, 0.28);
  background: rgba(64, 64, 68, 0.96);
  color: #fff;
  font-size: 12px;
  line-height: 18px;
}
.convert.hot {
  background: #ff2442;
  border-color: #ff2442;
  font-weight: 600;
}
.meter {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 40px;
  gap: 5px;
}
.bar {
  width: 4px;
  border-radius: 2px;
  background: #fff;
  transition: height 0.1s linear;
}
.hint {
  margin-top: 12px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.86);
}
.secs {
  margin-top: 6px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
}
</style>
