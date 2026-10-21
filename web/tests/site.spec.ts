import { test, expect } from '@playwright/test'

for (const width of [360, 390, 768, 1440]) {
  test(`public preparation page at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto('./')
    await expect(page).toHaveTitle(/AI vs オルカン/)
    await expect(page.getByRole('heading', { level: 1 })).toContainText('同じ100万円')
    await expect(page.getByText('仮想運用プロジェクト', { exact: true })).toBeVisible()
    await expect(page.getByText('実資金を使った運用ではありません。')).toBeVisible()
    await expect(page.getByText('Gemini · 現在はmock')).toBeVisible()
    await expect(page.getByText('JP-3 / 財務基盤', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.getByRole('link', { name: '実験の設計を見る' }).click()
    await expect(page).toHaveURL(/#experiment$/)
    await expect(page.locator('header a[href="https://github.com/okiNa4431/ai-trading-bot"]')).toBeVisible()
    expect(errors).toEqual([])
    await page.screenshot({ path: `test-results/site-${width}.png`, fullPage: true })
  })
}
