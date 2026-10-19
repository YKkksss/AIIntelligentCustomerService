import type { Directive } from 'vue'
import { useAuthStore } from '@/stores/auth'

/** `v-perm="'kb.write'"`：没有该权限就把元素从 DOM 里移除（不是隐藏，避免被 devtools 打开）。 */
export const perm: Directive<HTMLElement, string> = {
  mounted(el, binding) {
    const auth = useAuthStore()
    if (!binding.value || auth.can(binding.value)) return
    el.parentNode?.removeChild(el)
  },
}
