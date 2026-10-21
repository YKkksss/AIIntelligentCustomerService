<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { avatarDataUri, normalizeAvatar, type AvatarInput } from '@/utils/avatar'

const props = withDefaults(
  defineProps<{
    avatar?: AvatarInput | null
    /** 老数据没有头像时用应用 id 兜底 seed，保证同一应用永远同一张脸 */
    stableSeed?: string
    name?: string
    size?: number
    radius?: number
  }>(),
  { avatar: null, stableSeed: '', name: '', size: 40, radius: 10 },
)

const setting = computed(() => normalizeAvatar(props.avatar, props.stableSeed))
const src = ref('')
const failed = ref(false)

/** 首字母兜底：DiceBear 的 chunk 没加载下来（或未来传了坏 url）时不至于空着。 */
const initial = computed(() => (props.name.trim()[0] ?? 'A').toUpperCase())

let seq = 0
watch(
  () => [setting.value.style, setting.value.seed, props.size] as const,
  async ([, , size]) => {
    const current = ++seq
    failed.value = false
    if (setting.value.url) return
    try {
      const uri = await avatarDataUri(setting.value, Math.max(48, size * 2))
      if (current === seq) src.value = uri
    } catch {
      if (current === seq) failed.value = true
    }
  },
  { immediate: true },
)

const imageSrc = computed(() => setting.value.url || src.value)
</script>

<template>
  <span
    class="assistant-avatar"
    :style="{ width: `${size}px`, height: `${size}px`, borderRadius: `${radius}px` }"
  >
    <img v-if="imageSrc && !failed" :src="imageSrc" alt="" class="avatar-img" />
    <span v-else class="avatar-fallback">{{ initial }}</span>
  </span>
</template>

<style scoped>
.assistant-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex: none;
  background: var(--aics-pbg, #e6f4ff);
  color: var(--aics-primary, #1677ff);
  font-weight: 600;
}

.avatar-img {
  width: 100%;
  height: 100%;
  display: block;
}

.avatar-fallback {
  font-size: 0.45em;
  line-height: 1;
}
</style>
