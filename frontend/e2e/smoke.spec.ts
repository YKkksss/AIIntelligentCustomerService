import { expect, test } from '@playwright/test'

/**
 * M4 里程碑冒烟（ADR-0018）：登录 → 仪表盘 → 应用 → 用户 → 审计。
 * 需要真后端（模型调用只发生在上传/调试页，这里不触发，预算 0 元）。
 */
const EMAIL = process.env.AICS_E2E_EMAIL ?? 'admin@aisc.com'
const PASSWORD = process.env.AICS_E2E_PASSWORD ?? 'admin123456@'

test.skip(process.env.AICS_E2E !== '1', '设置 AICS_E2E=1 并先起后端 8010 / 前端 5174')

test('登录后关键页面可达', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByText('AICS 管理后台')).toBeVisible()
  await page.getByTestId('email').fill(EMAIL)
  await page.getByTestId('password').fill(PASSWORD)
  await page.getByRole('button', { name: /登\s*录/ }).click()

  // 仪表盘：KPI 四卡
  await expect(page.getByText('仪表盘').first()).toBeVisible()
  for (const label of ['对话量', '拒答率', '转人工率', '点踩率']) {
    await expect(page.getByText(label).first()).toBeVisible()
  }

  // 应用列表
  await page.getByRole('link', { name: '应用', exact: true }).click()
  await expect(page.getByText('新建应用')).toBeVisible()

  // 用户与审计
  await page.getByRole('link', { name: '用户', exact: true }).click()
  await expect(page.getByText('新建用户')).toBeVisible()
  await page.getByRole('link', { name: '审计', exact: true }).click()
  await expect(page.getByText('审计日志')).toBeVisible()
})
