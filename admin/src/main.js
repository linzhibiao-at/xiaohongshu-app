import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'

import { session } from './api'
import App from './App.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: () => import('./views/Login.vue') },
    {
      path: '/',
      component: () => import('./views/Layout.vue'),
      redirect: '/dashboard',
      children: [
        { path: 'dashboard', component: () => import('./views/Dashboard.vue') },
        { path: 'users', component: () => import('./views/Users.vue') },
        { path: 'plans', component: () => import('./views/Plans.vue') },
        { path: 'interactions', component: () => import('./views/Interactions.vue') },
        { path: 'plugins', component: () => import('./views/Plugins.vue') },
        { path: 'system', component: () => import('./views/System.vue'), meta: { admin: true } },
      ],
    },
  ],
})

router.beforeEach((to) => {
  if (to.path !== '/login' && !session.token) return '/login'
  if (to.meta.admin && session.user?.role !== 'admin') return '/dashboard'
})

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')
