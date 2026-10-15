import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

// access token 只存内存 + sessionStorage（关闭浏览器即失效），见管理后台设计 §1
const KEY = 'aics.access_token'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(sessionStorage.getItem(KEY))
  const isLoggedIn = computed(() => !!token.value)

  function setToken(value: string | null) {
    token.value = value
    if (value) sessionStorage.setItem(KEY, value)
    else sessionStorage.removeItem(KEY)
  }

  return { token, isLoggedIn, setToken }
})
