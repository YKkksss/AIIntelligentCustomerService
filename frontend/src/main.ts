import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import './styles/theme.css'
import router from './router'
import { perm } from './directives/perm'

const app = createApp(App)
app.use(createPinia()).use(router).use(ElementPlus, { locale: zhCn })
app.directive('perm', perm)
app.mount('#app')
