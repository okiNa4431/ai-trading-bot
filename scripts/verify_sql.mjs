// Run with a test-only PGlite module path; never connects to the user's Supabase.
import fs from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'
const modulePath = process.env.PGLITE_MODULE || path.resolve('.tools/sql/node_modules/@electric-sql/pglite/dist/index.js')
const { PGlite } = await import(pathToFileURL(modulePath).href)
const db = new PGlite()
await db.exec('create role anon; create role authenticated; create role service_role; grant usage on schema public to anon,authenticated,service_role;')
for (const file of (await fs.readdir('supabase/migrations')).sort()) {
  await db.exec(await fs.readFile(`supabase/migrations/${file}`, 'utf8'))
}
console.log('SQL migrations: PASS')
const account={account_id:'paper',created_at:'2026-10-15T00:00:00Z',last_event_at:null,
base_currency:'JPY',initial_jpy:'1000000',balances:{JPY:'1000000',USD:'0'},positions:{},realized_pnl:{JPY:'0',USD:'0'}}
const initial={kind:'cash_ledger',key:'INITIAL:initial:JPY',payload:{entry_id:'INITIAL:initial:JPY',event_id:'initial',currency:'JPY',amount:'1000000',balance_after:'1000000',kind:'INITIAL',timestamp:account.created_at}}
const commit=async(id,version,value,writes)=>db.query('select public.bot_commit($1,$2,$3,$4,$5) v',[id,id.startsWith('test-'),version,value,writes])
await db.exec('set role service_role')
await commit('test-local-sql',0,account,[initial])
let r=await db.query("select public.bot_read('test-local-sql') value")
assert.equal(r.rows[0].value.account.balances.JPY,'1000000')
await assert.rejects(()=>commit('test-local-sql',0,account,[]),/version conflict/)
const bad={...account,balances:{JPY:'999999',USD:'0'}}
await assert.rejects(()=>commit('test-local-sql',1,bad,[]),/ledger/)
r=await db.query("select public.bot_read('test-local-sql') value")
assert.equal(r.rows[0].value.version,1)
assert.equal(r.rows[0].value.account.balances.JPY,'1000000')
await db.exec('reset role; set role anon')
await assert.rejects(()=>db.query('select * from bot_private.accounts'),/permission denied/)
await assert.rejects(()=>db.query("select public.bot_read('test-local-sql')"),/permission denied/)
await assert.rejects(()=>db.query("delete from public.bot_dashboard"),/permission denied/)
assert.equal((await db.query('select * from public.bot_dashboard')).rows.length,0)
await db.exec('reset role; set role authenticated')
await assert.rejects(()=>db.query('select * from bot_private.records'),/permission denied/)
await assert.rejects(()=>db.query("insert into public.bot_dashboard values ('paper-live','{}',now())"),/permission denied/)
await db.exec('reset role; set role service_role')
await commit('paper-live',0,account,[initial])
await db.query('select public.bot_publish($1,$2,$3)',['paper-live',1,{schema_version:1,state:'PREPARING'}])
await db.exec('reset role; set role anon')
assert.equal((await db.query('select document from public.bot_dashboard')).rows[0].document.state,'PREPARING')
console.log('SQL atomic commit / stale CAS / rollback / anon + authenticated privileges / public read: PASS')
await db.close()
