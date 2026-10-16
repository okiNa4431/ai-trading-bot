import {test, expect} from '@playwright/test'

const document={schema_version:1,state:'PREPARING',updated_at:'2026-10-16T12:00:00Z',started_at:'2026-10-15T00:00:00Z',
 summary:{bot_jpy:'1000000',benchmark_jpy:null,bot_return:'0',benchmark_return:null,excess_return:null,maximum_drawdown:'0',trade_count:0},
 series:[{at:'2026-10-16T12:00:00Z',bot_jpy:'1000000',bot_return:'0',benchmark_jpy:null,benchmark_return:null,complete:false,warnings:[]}],
 holdings:[],trades:[],decisions:[],usage:{models:[],requests:0,decision_count:0,input_tokens:null,output_tokens:null,total_tokens:null,estimated_cost_usd:null},warnings:[]}
for(const width of [360,390,768,1440]){
 test(`dashboard empty state, tabs and no overflow at ${width}`,async({page})=>{
  await page.setViewportSize({width,height:900})
  await page.route('**/runtime-config.json',r=>r.fulfill({json:{url:'',key:''}}))
  await page.goto('./')
  await expect(page.getByText('まだ運用開始前',{exact:true})).toBeVisible()
  await expect(page.getByText('公開データを準備しています。架空の運用成績は表示しません。')).toBeVisible()
  for(const [label,message] of [['保有銘柄','保有銘柄はありません。'],['売買履歴','まだ約定した取引はありません。'],['AI判断','まだ保存された判断はありません。']]){
   await page.getByRole('button',{name:label,exact:true}).click()
   await expect(page.getByText(message,{exact:true})).toBeVisible()
  }
  await page.getByRole('button',{name:'概要',exact:true}).click()
  expect(await page.evaluate(()=>globalThis.document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:`test-results/dashboard-empty-${width}.png`,fullPage:false})
 })
}
test('public projection only, same-day missing NAV stays blank, chart table accessible',async({page})=>{
 await page.route('**/runtime-config.json',r=>r.fulfill({json:{url:'https://public-example.invalid',key:'sb_publishable_test'}}))
 await page.route('https://public-example.invalid/**',async r=>{
  expect(r.request().method()).toBe('GET')
  expect(r.request().url()).toContain('/rest/v1/bot_dashboard?')
  expect(r.request().headers()['apikey']).toBe('sb_publishable_test')
  await r.fulfill({json:[{document}]})
 })
 await page.goto('./')
 await expect(page.locator('.metric-value').first()).toHaveText('￥1,000,000')
 await expect(page.locator('.metric-value').nth(1)).toHaveText('—')
 await expect(page.getByRole('img',{name:'AIとオルカンの累積リターン'})).toBeVisible()
 await page.getByText('評価値を表で確認').click()
 await expect(page.getByRole('columnheader',{name:'基準日時 JST'})).toBeVisible()
 await page.getByRole('button',{name:'API利用',exact:true}).click()
 await expect(page.getByRole('heading',{name:'API利用状況'})).toBeVisible()
 await expect(page.locator('.usage-grid')).toContainText('不明')
})
test('refresh failure preserves last successful update and values',async({page})=>{
 await page.route('**/runtime-config.json',r=>r.fulfill({json:{url:'https://public-example.invalid',key:'sb_publishable_test'}}))
 let calls=0
 await page.route('https://public-example.invalid/**',r=>++calls===1?r.fulfill({json:[{document}]}):r.fulfill({status:503,body:'private internal error'}))
 await page.goto('./')
 await expect(page.locator('.metric-value').first()).toHaveText('￥1,000,000')
 await page.getByRole('button',{name:/最新データに更新/}).click()
 await expect(page.getByRole('alert')).toContainText('最後に取得した情報')
 await expect(page.locator('.metric-value').first()).toHaveText('￥1,000,000')
 await expect(page.locator('body')).not.toContainText('private internal error')
})
test('malformed public values fail safely without invented results',async({page})=>{
 await page.route('**/runtime-config.json',r=>r.fulfill({json:{url:'https://public-example.invalid',key:'sb_publishable_test'}}))
 await page.route('https://public-example.invalid/**',r=>r.fulfill({json:[{document:{...document,series:[{at:'broken',bot_jpy:'NaN'}]}}]}))
 await page.goto('./')
 await expect(page.getByRole('alert')).toBeVisible()
 await expect(page.locator('.metric-value').first()).toHaveText('—')
})
test('AI text is rendered as text, no script execution or fabricated fills',async({page})=>{
 await page.route('**/runtime-config.json',r=>r.fulfill({json:{url:'https://public-example.invalid',key:'sb_publishable_test'}}))
 const history={...document,decisions:[{at:document.updated_at,market:'JP',provider:'mock',status:'SUCCESS',registered_count:0,
  items:[{symbol:'TEST.T',action:'HOLD',reason:'<img src=x onerror=alert(1)>',skipped:'HOLD: no order'}]}]}
 await page.route('https://public-example.invalid/**',r=>r.fulfill({json:[{document:history}]}))
 await page.goto('./')
 await page.getByRole('button',{name:'AI判断',exact:true}).click()
 await expect(page.getByText('<img src=x onerror=alert(1)>',{exact:true})).toBeVisible()
 expect(await page.locator('.decision-item img').count()).toBe(0)
 await expect(page.getByText('見送り：HOLD: no order')).toBeVisible()
})
test('invalid holding currency is rejected before rendering financial values',async({page})=>{
 await page.route('**/runtime-config.json',r=>r.fulfill({json:{url:'https://public-example.invalid',key:'sb_publishable_test'}}))
 await page.route('https://public-example.invalid/**',r=>r.fulfill({json:[{document:{...document,
  holdings:[{symbol:'TEST',name:'invalid',currency:'secret-not-a-currency',quantity:1,average_cost:'1',market_value_jpy:'1',unrealized_pnl:'0',ratio:'.1',at:document.updated_at}]}}]}))
 await page.goto('./')
 await expect(page.getByRole('alert')).toBeVisible()
 await page.getByRole('button',{name:'保有銘柄',exact:true}).click()
 await expect(page.getByText('保有銘柄はありません。',{exact:true})).toBeVisible()
 await expect(page.locator('body')).not.toContainText('secret-not-a-currency')
})
