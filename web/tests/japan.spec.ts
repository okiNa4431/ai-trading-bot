import { test, expect } from '@playwright/test'

for (const width of [360, 1440]) {
  test(`JP comparison separates preparation and SYNTHETIC demo at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    const external: string[] = []
    page.on('request', r => { if (!r.url().startsWith('http://127.0.0.1')) external.push(r.url()) })
    await page.goto('./')
    await expect(page.getByRole('heading', { level: 1 })).toContainText('日本株AI')
    await expect(page.locator('.roadmap')).toContainText('実装済み／実データ検証待ち：通信ゲートは無効')
    await expect(page.locator('.roadmap')).toContainText('完了：mock選定')
    const live = page.getByRole('region', { name: '日本株実運用の準備状況' })
    await expect(live).toContainText('実市場データによる売買実績はありません。')
    await expect(live.locator('.jp-placeholder')).toHaveCount(4)
    await expect(page.getByRole('region', { name: '仮想運用ダッシュボード' })).toHaveCount(0)
    await page.getByRole('button', { name: 'SYNTHETIC比較デモを見る' }).click()
    const demo = page.getByRole('region', { name: '合成データの比較デモ' })
    await expect(demo.locator('.jp-synthetic-label')).toContainText('SYNTHETIC')
    await expect(demo.getByRole('img')).toHaveAttribute('aria-label', /SYNTHETIC/)
    await expect(demo).toContainText('実API通信0回')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: `test-results/japan-${width}.png`, fullPage: true })
    expect(external).toEqual([])
  })
}
